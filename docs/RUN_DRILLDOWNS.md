# Opening a run: the transcript, and what stands in for it

Clicking a row in the runs table opens one of two things. Most rows open a
**transcript** — what the agent said and did. Some rows have no transcript to
open, and they get **details** instead: the run record plus the definition of
the monitor that produced it.

Which one a row gets is decided by data, not by kind: a row with a `session_id`
has a transcript, a row without has details (see
[RUNS_VIEW.md](RUNS_VIEW.md#row-identity-is-three-fields-not-one)).

## `GET .../subagents/{session}/transcript`

There are now two readers over the same transcript file, and the difference is
what each throws away.

`/messages` is the **chat view**: two roles, prose only. That is what a
conversation panel wants, and it is unchanged — every existing caller keeps its
shape.

`/transcript` is the **debugging view**. Debugging a run means asking *when did
each turn happen* and *what did the agent actually do between saying things*,
so this one keeps timestamps and tool calls:

```json
{"session": "review-worker-3",
 "entries": [
   {"kind": "message", "role": "agent", "text": "looking",
    "at": "2026-08-01T12:00:00.000Z", "tool": "",
    "truncated": false, "is_error": false},
   {"kind": "tool", "role": "agent", "text": "ls -la /tmp",
    "at": "2026-08-01T12:00:00.000Z", "tool": "Bash",
    "truncated": false, "is_error": false}],
 "usage": {"started_at": 0.0, "ended_at": 0.0, "tokens": 0,
           "cost_usd": 0.0, "status": "completed"}}
```

`kind` is `message`, `tool`, or `tool_result`. Every entry carries every key, so
the renderer branches on value and never on key presence. `usage` feeds the
slab header.

A few deliberate choices:

**A tool call is summarized, not dumped.** `text` on a `tool` line is a one-line
gist of the call's input — the command, the file path, the query — because the
slab wants "which file, which command", not a pretty-printed argument tree. The
summary names the telling key when there is one and falls back to the input's
key list, which at least says what shape the call had.

**A tool result is previewed.** Results can be entire files, so they clip at
`TOOL_RESULT_PREVIEW` (400 chars) with `truncated: true`. The transcript on disk
stays the place to read one whole. `is_error` marks a failed call.

**A missing timestamp stays missing.** `at` is empty where the on-disk format
records none — Codex rollouts do not carry per-entry timestamps in this shape.
Substituting the read time would date every old line to whenever the page was
opened, which is worse than an empty column.

## The composer: replying from the transcript

A transcript slab carries a reply box at its foot. It is the page's only
typing surface besides the runs search, and it has **two branches**, chosen by
`detail.live` ([RUNS_VIEW.md](RUNS_VIEW.md#detaillive-can-this-row-be-spoken-to-right-now))
rather than by the row's kind:

| `detail.live` | What the box says | What sending does |
|---|---|---|
| true | Send | `POST .../chat` with `subagent: <session_id>`. The text is delivered to that session through `inbox.deliver` - the same function `bobi agent <name> message` reaches from a terminal. |
| false | Continue in a new session | `POST .../chat` with an empty `subagent`, which means the team manager. The text travels inside a relay naming the ended session and its run, as a request to carry the work forward in a FRESH session. |

Both use the endpoint pair that already existed; neither adds one.

**Why a finished row does not simply refuse.** A suspended gate has no process:
the orchestrator disconnects its client and returns at the await step, so the
session exits while its registry entry stays `waiting`. `deliver` refuses a
dead pid, and a terminal cannot reach those rows either. The continuation
branch is what the operator gets instead, and it is a request rather than a
spawn - the manager is an agent, and it decides.

**A reply re-reads the transcript; a continuation does not.** The slab is
otherwise one-shot (the 4s timers refresh the table, not this), so the live
branch re-fetches `/transcript` when the turn lands or the answer would be
invisible until the run was reopened. The continuation's reply lands in the
*manager's* transcript, not this dead session's, so re-rendering this one
would read as the message having been swallowed. It reports where the message
went instead, and the new run appears in the table on its own.

**Nothing here resumes a workflow run, by any route.** A suspended run records
`suspended_at_step = step_idx + 1` - the step *after* its gate - and
`resume_workflow` feeds that straight back as the starting step. Resuming a
run parked on an approval therefore skips the approval and starts the next
step. `agent.js` has no call to the resume route, gains none, and
`tests/test_webapp_composer.py` fails if it ever does. The relay message says
the same thing in words, because the manager *can* resume even though the page
cannot.

## `GET .../runs/{run_id}/details`

For a run with no session. Two of them are ordinary, not broken: a `$0`
script-cache monitor tick never spawned an agent — that is the cheap path
working — and a firing that failed before its agent started never got one
either.

```json
{"kind": "monitor",
 "run": {"run_id": "...", "monitor": "inbox-watch", "outcome": "quiet",
         "reason": "", "flavor": "check:script_cache",
         "script_cache_mode": "cached", "session_ref": "", "published": 0,
         "started_at": "...", "ended_at": "..."},
 "definition": {"name": "inbox-watch", "interval": "15m",
                "description": "watch the inbox", "event": "monitor/inbox"},
 "session_id": ""}
```

**The pairing is the point.** The record says what happened on this firing; the
definition says what the monitor was asked to do. Debugging a monitor almost
always means holding both, and the definition is otherwise only visible by
reading `agent.yaml` on the box.

**The definition is a curated subset, not `to_dict()`.** A monitor's `command`
can carry credentials interpolated into it, and this is a debugging view, not a
config dump — so the command is not in the payload.

**A record outlives its monitor.** A firing recorded last week is still worth
reading after the monitor was renamed or deleted, so a missing definition costs
that half of the slab and returns `{}` — never the response.

**`session_id` is present even here.** A firing that *did* spawn a session has
both a record and a transcript, and the slab should offer the transcript rather
than pretend the row has nothing more to show.

An id with no record is `404 unknown run` — an ordinary outcome once a record
ages past the retention cap while a browser still holds its row, which is why
`UnknownRun` is its own runtime error rather than a generic lifecycle failure.

## Compatibility

`TeamRuntime.transcript()` and `run_details()` **are** `@abstractmethod`, and
the base carries no fallback. Both implementers live in this repo, so a
missing one fails CI here rather than somewhere nobody is looking.

The hosted side answers `run_details` from its own admin command, and
`transcript` from the existing `transcript` command widened with
`detail: true`. That widening matters for the distinction this document
draws: `messages` is the chat view and has already discarded every tool call,
so the debugging view cannot be derived from it downstream — it has to be read
from the transcript on the box. A supervisor too old to know the arg replies
without `entries`, and the hosted runtime reports that as unavailable rather
than rendering a debugging view with the tool calls silently missing.
