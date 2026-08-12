# 987 - An inline reply box on the awaiting-action Details slab

Status: **awaiting Gate 1** (design approval). No code written.
Issue: [#987](https://github.com/moda-labs/bobi-agent/issues/987) · Linear MOD-372 (part of MOD-368)
Author: engineer agent, 2026-08-12
Base: `main` @ `8441de2`

---

## 1. Summary

The issue asks for a reply box at the bottom of the opened slab so an operator can answer a workflow gate without leaving the agent page.
It also asks a design question first: does the typed text get parsed/mapped to the gate's `await_event`, or does it get posted into the Slack thread so existing reply-parsing handles it?

**Both halves of that question rest on things that do not exist.**

- There is no reply-parsing to hand off to. The only event-driven resume entry point, `try_resume_for_event`, has **no production caller** (§3.1). A Slack reply has never advanced a gate.
- There is nothing to parse. Every waiting run records exactly **one** `await_event`, and the page already displays it. All 8 gates waiting in this deployment right now await the same event, `approval` (§3.2).

So the fork is not (a) parse vs (b) forward.
It is: **does the page get an approve action, or does it not?**
Post-#989 the page can *abandon* a gate (Close) but cannot *pass* one, and force-resume was deliberately taken out of the UI (§3.6).

I recommend **Option A: reply-and-continue** (§5.1). The typed text rides as the resumed run's `event` payload, which `resume_workflow` already accepts and injects (orchestrator.py:410), so the frozen engine needs no change.
It must be **labelled as approval, not as a reply**, because that is mechanically what it is, and `docs/RUN_RESUME.md` is owed an amendment saying so.

I am also flagging one thing the issue did not ask about but that changes the feature's value more than anything else in it: `awaiting_action` is a **24-hour-derived** status, so a box gated on it is invisible for the first day of every gate's life (§3.5). Gate it on `detail.resumable` instead.

---

## 2. Baseline this is specced against

The issue body is stale in two places. Verified against `main` @ `8441de2`:

| Issue says | Actually |
|---|---|
| "gives the operator only **Remind** and **Close**" | Correct today (agent.js:619-635), but sibling **#989** (MOD-371) is removing Remind now. **Post-#989 the row offers only Close.** |
| "see the comment at `agent.js:241-243`" | Those lines are the telemetry tile loop and the header actions span. No chat comment. The real one is **server.py:353-354** (`"/messages above is the chat view and stays exactly as it was"`), plus agent.js:10-12 (`"Chat lives in Slack and the CLI"`). §3.4 traces where that path actually goes. |
| `rowActions()` "lines ~619-636" | `rowActions()` is agent.js:597-637; the `awaiting_action` branch is 619-635. |

Siblings on the same file, same base: **#988** (MOD-373) edits `renderSaved()` (agent.js:315-359), **#989** edits `rowActions()`/`remind()` (agent.js:597-658).
This work touches `openSlab()`/`renderTranscript()`/`renderRowDetails()` (agent.js:691-794) and re-enters `rowActions()` only if Gate 1 picks the row-button variant in §5.4.
**#989 is the merge-order dependency**: it deletes lines inside `rowActions()` that this feature reads around.

That the row is down to a single Close button is not a footnote. It is the motivation:

> After #989, the only thing the agent page can do to a human approval gate is kill it.

---

## 3. What actually happens today

Every claim below was derived in a worktree off `main` @ `8441de2`. The greps are printed so they can be re-run.

### 3.1 A Slack reply does not, and never did, advance a gate

```
$ grep -rn "resume_workflow\|\.claim()" --include=*.py bobi/
bobi/cli.py:2409:    from .workflow.orchestrator import resume_workflow
bobi/cli.py:2426:    if not run.claim():
bobi/cli.py:2439:    success = resume_workflow(run, wf, timeout=timeout)
bobi/workflow/orchestrator.py:176:    resume path. Before wiring one up: resume_workflow re-stamps the registry
bobi/workflow/orchestrator.py:190:    if not run.claim():
bobi/workflow/orchestrator.py:206:        target=resume_workflow,
bobi/workflow/orchestrator.py:291:    and asks for it here. ``resume_workflow`` deliberately never sets it.
bobi/workflow/orchestrator.py:372:def resume_workflow(
bobi/webapp/run_actions.py:71:    (``try_resume_for_event``): ``resume_workflow`` re-stamps the session
bobi/webapp/server.py:202:    def resume_workflow_run(name: str, run_id: str) -> JSONResponse:
```

Classifying every hit:

| Hit | What it is |
|---|---|
| cli.py:2409, 2426, 2439 | **Live path.** `bobi agent <n> workflows resume <run_id>` (cli.py:2395-2441). Claims, then resumes. |
| orchestrator.py:190, 206 | Inside `try_resume_for_event` (orchestrator.py:165-213). **Dead**: see below. |
| orchestrator.py:176, 291, 372; run_actions.py:71 | Definition and docstrings, not calls. |
| server.py:202 | The HTTP route, which delegates to `run_actions.resume_run` (run_actions.py:67-99), which **spawns the cli.py command**. Not a second implementation. |

`try_resume_for_event` is the only function that maps an inbound *event* to a waiting run, and it says of itself:

> orchestrator.py:175-177
> `No production caller today; the CLI `workflows resume` is the only live resume path.`

The shipped doc agrees:

> docs/WORKFLOW_ENGINE.md:395-399
> `Resume is **manual today**: the event-driven entry point try_resume_for_event(...) exists in bobi/workflow/orchestrator.py and is covered by tests, but nothing in the runtime calls it, so a run that suspends on await: stays waiting even once its awaited event arrives.`

What a Slack reply *does* travel through today, traced end to end:
Slack gateway → event server → drain loop (`bobi/events/drain.py`) → `EventReactor.process()` (reactor.py:172-215) → either **launch a new workflow** (`_dispatch` → `launch_agent`, reactor.py:227-281) or fall through to the manager session's inbox for the LLM to read.
`EventReactor` has no branch that looks at waiting runs. Nothing on that path calls `find_waiting`, `claim()`, or `resume_workflow`.

**So option (b) as the issue words it cannot be built, because the thing it delegates to is not there.**

### 3.2 There is nothing to parse

`await_event` is a single recorded string per run (state.py:37), rendered on the row (agent.js:511-513) and in the slab (agent.js:778).
The runs read model already ships it in `detail` (runs.py:220-224).

Live evidence from this deployment's own `state/workflow/runs/`:

```
total run records: 9    status counts: {'waiting': 8, 'running': 1}

  run_id     workflow          await       run_key             requested_by
  46aecc34   issue-lifecycle   'approval'  'adhoc-60c70cd8'    {channel: C0BAEN48KQR, thread_ts: 1785885345.077239, ...}
  409b4300   issue-lifecycle   'approval'  '933'               {}
  2a6c8c9e   issue-lifecycle   'approval'  '958'               {}
  7eec97d1   issue-lifecycle   'approval'  '1006'              {}
  b0529f37   issue-lifecycle   'approval'  'adhoc-57133a39'    {channel: C0BAEN48KQR, thread_ts: 1785602189.939359, ...}
  e097927a   issue-lifecycle   'approval'  'adhoc-e0dc9a95'    {}
  75881342   issue-lifecycle   'approval'  'adhoc-016d4503'    {}
  e5f2573a   issue-lifecycle   'approval'  'adhoc-55df2bbc'    {channel: C0BAEN48KQR, thread_ts: 1785444244.355699, ...}
```

Reproduce:

```bash
python3 - <<'PY'
import json, glob, collections
rows = [json.loads(open(p).read())
        for p in glob.glob('<run>/state/workflow/runs/*.json')]
print(collections.Counter(r.get("status") for r in rows))
for r in rows:
    if r.get("status") != "waiting": continue
    rb = (r.get("variable_scopes") or {}).get("requested_by") or {}
    print(r["run_id"], r["workflow_name"], repr(r.get("await_event")),
          repr(r.get("run_key")), rb.get("channel", "-"), r.get("session_name"))
PY
```

Eight waiting gates, one distinct `await_event` between them.
`agents/eng-team/workflows/issue-lifecycle.yaml:76-77` (`await: approval`) is the **only** `await:` gate in the repo:

```
$ grep -rn "await:" --include=*.yaml --include=*.yml .
agents/eng-team/workflows/issue-lifecycle.yaml:77:    await: approval
```

A classifier that picks between one option is not a classifier.
**Option (a)'s "parse/map to the event" problem does not exist either.** The run already names its event; the only thing the operator's text can be is a *payload*.

### 3.3 Five of the eight live gates have no Slack thread to post into

`_execute_notify_step` resolves its destination from the `requested_by` scope, and gives up when there is none:

```
orchestrator.py:1431-1436
    requester = ctx.scopes.get("requested_by") or {}
    channel = requester.get("channel", "")
    thread_ts = requester.get("thread_ts", "")
    if not channel:
        return _undeliverable("no Slack channel available")
```

`requested_by` is populated only when a launcher passes it. `reactor._dispatch` does not:

```
reactor.py:264-271
                bobi.subagent.launch_agent(
                    task=task,
                    cwd=self.cwd,
                    workflow_name=rule.workflow,
                    role=rule.role,
                    run_key=run_key,
                    input_fields=input_fields,
                )
```

`launch_agent`'s `requested_by` defaults to `None` (subagent.py:1026-1031) and is normalised to `{}` downstream.
That matches the table in §3.2 exactly: the GitHub-dispatched runs (`run_key` = issue number 933 / 958 / 1006) carry `{}`; the Slack-launched ones carry a channel.

**5 of 8 waiting gates right now have no address for option (b) to post to.** For those runs `remind_run` also cannot work, which is consistent with #989's report that Remind does nothing.

### 3.4 The `/messages`-style chat plumbing provably cannot reach a gate's session

The issue points at kept-for-compatibility chat plumbing and says to reuse it. Traced:

`POST /api/agents/{name}/chat` (server.py:375-384) → `rt.chat_submit` (runtime.py:687-708) → `service.ask(root, session, text)` (runtime.py:698).

```
service.py:803-804  (inside ask, defined at 789)
    if agent not in {e.name for e in list_agents(project_path)}:
        raise MessageDeliveryError(f"unknown agent '{agent}'")
```

- `service.list_agents` → `SessionRegistry.list_active()` (service.py:734, 739)
- `list_active` keeps only `ACTIVE_STATUSES` (sdk.py:44 = `("starting", "running", "idle")`)
- on suspend the orchestrator sets the session's registry status to `waiting` (orchestrator.py:867)

`waiting` is not in `ACTIVE_STATUSES`, so **`ask()` on a suspended gate's session raises `unknown agent`**.
And there is no client-side chat surface left to reuse: `bobi/webapp/static/views/` is `agent.js`, `dashboard.js`, `markdown.js`, and `grep -rn "messages" bobi/webapp/static/` returns nothing.

Even if delivery worked, the receiving agent is not told to resume anything: `grep -rn "resume" bobi/prompts/base.md` returns no hits.

**The pointer is honest about the plumbing existing; it just does not lead anywhere useful for a gate.** Recorded so nobody re-derives it.

### 3.5 `awaiting_action` arrives 24 hours late

```
bobi/webapp/runs.py:46   AWAITING_ACTION_AFTER_SECONDS = 24 * 3600
```

```
runs.py:178-190  (_workflow_status)
    if run.status in ("waiting", "suspended"):
        started = _epoch(run.resumed_at or run.started_at)
        if started and (now - started) >= AWAITING_ACTION_AFTER_SECONDS:
            return AWAITING_ACTION
        return IDLE
```

Documented at docs/RUNS_VIEW.md:52-56 ("`awaiting_action` is derived, not recorded").

A gate that suspended ten minutes ago is `idle`. It gets **no** gate actions on the row today, and under the issue's literal wording ("for `awaiting_action` rows") it would get **no reply box for its first 24 hours** either.
That inverts the feature: the operator most likely to answer a gate is the one who just watched it open.

The read model already carries the right predicate, and the frontend ignores it:

```
runs.py:224            "resumable": run.status == "waiting"
$ grep -rn "resumable" bobi/webapp/static/     # → no hits
```

**Gate the reply box on `detail.resumable`, not on `row.status === "awaiting_action"`.** No read-model change needed.

### 3.6 Reply-and-continue *is* force-resume, and that was removed on purpose

```
docs/RUN_RESUME.md:3-5
> The agent UI no longer exposes force-resume. Awaiting-action rows resend the
> original gate notification or close the workflow. The endpoint below remains
> available to the CLI/framework for explicit force-continuation.
```

```
docs/RUN_RESUME.md:36-39
The payload names the workflow and the awaited event because the UI confirms
before firing: `workflows resume` force-continues the suspended step, and on an
`await: approval` gate that proceeds *as if approved*. The confirm has to say
what is about to be waved through.
```

The engine has no reject. `resume_workflow` re-enters at `suspended_at_step` unconditionally (orchestrator.py:372-415); `close_run` cancels and never runs later steps (run_actions.py:123-142).
So **any** control that continues a gate is force-resume wearing a different label, whatever text rides along.

This spec does not smuggle it back in. It states it: the objection in RUN_RESUME.md is to a bare **Resume** button that is really an unlabelled approval, and the fix it demands is honest confirm language, not a permanent ban (the endpoint is explicitly kept).
Option A satisfies that by naming the control **Approve and continue** and carrying the workflow + step + awaited event in the confirm.
If Gate 1 disagrees, that is exactly the call Gate 1 exists to make, and §5.5 is the do-nothing branch.

Two doc-consistency items fall out, both fixed in the implementing PR:

1. `docs/RUN_RESUME.md:20-22` still calls resume "the runs table's one write action", which contradicts its own header note at lines 3-5. Stale since force-resume left the UI.
2. `docs/WORKFLOW_ENGINE.md:401` says the checklist plan "proposes removing the await/resume feature rather than repairing it". The plan's re-approval superseded that: `plans/2026-07-26-checklist-execution-model.md` is **Approved (re-approved 2026-07-29)** and its thesis is that **"the engine is frozen, not deleted"**. Await/resume is frozen, not slated for removal. That matters here: a frozen engine is an argument for keeping this change in the webapp layer, which Option A does.

### 3.7 The reminder text already promises what the system cannot do

```
orchestrator.py:107-112
        notify_step = StepDef(
            name=f"remind_{await_step.name}", notify="slack",
            message=(f"Reminder: {run.workflow_name}{subject} is waiting for "
                     f"your {action}. Reply in this thread to continue, or "
                     "close the workflow from Bobi."),
        )
```

Replying in that thread continues nothing (§3.1). Whatever Gate 1 decides, this string is wrong and gets corrected in the implementing PR.

---

## 4. The design question, answered

> Does the reply need to be parsed/mapped to the `await_event` (same handling Slack replies get today), or is this a shortcut that posts into the Slack thread and lets existing reply-parsing handle it?

**Neither, and the premise of each is false.**

| | Verdict | Why |
|---|---|---|
| (a) parse/map the text to the `await_event` in the webapp | **Not needed** | One recorded event per run (§3.2). Nothing to disambiguate. The text is a payload, not a selector. |
| (b) post to the Slack thread, let existing parsing handle it | **Not possible** | No such parsing exists (§3.1). And 5/8 live gates have no thread (§3.3). |

The real question, and the one Gate 1 should answer:

> **Should the agent page be able to pass a human approval gate, and if so, is a typed note the right shape for the payload?**

Concretely, the typed text would travel: reply box → `POST .../runs/{run_id}/reply` → `run_actions.reply_run` → spawn `bobi agent <name> workflows resume <run_id> --event-data '{"text": "..."}'` → `resume_workflow(event=...)` → `ctx.set_scope("event", event["data"])` (orchestrator.py:410) → readable by later steps as `${{ event.text }}`.
No parsing, no classification, no Slack round-trip.

---

## 5. Options

### 5.1 Option A - reply-and-continue (RECOMMENDED)

The box resumes the run and carries the text as the resumed run's `event` payload.

- **Works for all 8 live gates**, including the 5 with no Slack thread.
- **Zero engine change.** `resume_workflow` already takes `event` and injects it (orchestrator.py:410). The engine is frozen (§3.6); this respects that.
- Additions: one `--event-data` flag on `cli.py:2395` `workflows resume`, one `reply_run` in `run_actions.py`, one route, one `TeamRuntime` method on each of the three implementations (LocalRuntime / EventBusRuntime / supervisor admin), one UI block.
- **Cost, stated plainly**: it re-opens a UI affordance RUN_RESUME.md closed. Mitigated by naming and confirm copy (§6), not by pretending otherwise.
- Risk: an operator reads "reply" as "send a message" and gets "workflow proceeds". **The label must say approve.** See §6.

### 5.2 Option B - post into the gate's Slack thread

Reuse `requested_by` + `post_slack_message` the way `remind_run` does.

- Delivers a visible message. **Does not advance the gate.**
- **Fails outright for 5/8 live gates** (§3.3) with `no Slack channel available`.
- Makes §3.7's false promise worse: the operator now types into Bobi, sees it land in Slack, and still nothing happens.
- Rejected. It is a strictly more expensive way to reach today's outcome.

### 5.3 Option C - reuse the `/chat` plumbing

The issue's explicit pointer. **Provably dead**: `ask()` rejects the suspended session (§3.4). Rejected on mechanism, not on taste.

### 5.4 Sub-decision inside Option A: where the control lives

The issue says "bottom of the opened slab". Two wrinkles:

1. **All 8 live waiting runs carry a `session_name`** (§3.2), and `runs.py:215` sets `session_id=run.session_name`. So `openSlab()` takes the **transcript** branch (agent.js:700-707), not `renderRowDetails` (agent.js:766-782). A box written only into the Details renderer would never appear on a real gate. It must attach after **either** renderer, in `openSlab()`, keyed on `detail.resumable`.
2. The slab opens on row click **and** on the Transcript/Details buttons (agent.js:545-551, 603-616), so the box is reachable either way. No row-button variant is required. Recommending slab-only keeps `rowActions()` untouched and removes the #989 merge collision.

**Recommendation: slab-only, appended in `openSlab()` after the renderer returns.**

### 5.5 Option D - decline, and fix only the false promise

If Gate 1 holds that the page must not pass gates: keep Close as the only write, and still land §3.6's two doc fixes plus §3.7's message correction.
Small, honest, and leaves the operator with Slack or the CLI. Named so the decision is a real choice.

---

## 6. UX, per the design system

`docs/design-system/README.md:369-372` is binding and non-negotiable here:

> **Gates are sacred.** A human approval step always renders with the violet left rail + 4% wash + rotated-square glyph, always names the workflow and step that stopped, and is never downgraded to a toast or an auto-dismissing dialog.

The block is therefore a **gate**, not a chat composer:

- Violet left rail, 4% wash, rotated-square glyph. Violet is state, never decoration.
- Names the workflow, the step, and the awaited event, from `row.title`, `detail.suspended_at_step`, `detail.await_event`. All three are already in the row payload (runs.py:220-224); no new fetch.
- Label: **Approve and continue** (primary), with the textarea labelled as a note carried to the next step. Not "Send". Not "Reply".
- Confirm before firing, in RUN_RESUME.md:36-39's terms: name the workflow and step, and say that continuing proceeds *as if approved* and that later steps will run.
- Failure renders inline in the slab (the `slabError`/`showReport` idiom, agent.js:291-296, 724-727). Never a toast.
- Chrome is lowercase; mono for the run id / event name (data), sans for prose.
- The 409 case ("no longer waiting", run_actions.py:63) is a normal outcome, not an error shout: the run moved, so re-poll and re-render.

Open UX question for Gate 1: whether the note should be **required** or optional. Required makes the operator state a reason and produces a better audit trail; optional makes the common "yes, go" case one click. I lean optional with a placeholder that says the note reaches the next step.

---

## 7. Scope

**In**

- `bobi/webapp/static/views/agent.js`: gate block appended in `openSlab()` after either renderer, gated on `detail.resumable`; submit, confirm, inline error, re-poll.
- `bobi/webapp/static/app.css`: gate block styling from existing tokens.
- `bobi/webapp/run_actions.py`: `reply_run(root, run_id, text)`, sharing `_waiting_run` with the other three.
- `bobi/webapp/server.py`: `POST /api/agents/{name}/workflows/runs/{run_id}/reply`.
- `bobi/webapp/runtime.py` (`TeamRuntime` + `LocalRuntime`), `bobi/webapp/event_bus.py`, `bobi/supervisor/admin.py`: `reply_run` alongside the existing trio, so local and hosted stay one implementation (run_actions.py:8-12).
- `bobi/cli.py`: `--event-data` on `workflows resume`.
- Docs in the same PR: `docs/RUN_RESUME.md` (the new action + the §3.6 item 1 correction), `docs/RUN_DRILLDOWNS.md` (the slab now has a write surface), `docs/WORKFLOW_ENGINE.md:401` (§3.6 item 2), `docs/RUNS_VIEW.md:55` (row actions after #989).
- `bobi/workflow/orchestrator.py:107-112`: correct the reminder's false promise (§3.7).

**Out**

- Wiring `try_resume_for_event`. Its own docstring (orchestrator.py:175-182) says a thread-based resume would stamp the wrong pid, and the engine is frozen. Not this ticket.
- Any approve/**reject** vocabulary in the engine. The engine has no reject (§3.6); adding one is an engine change against a frozen engine.
- Backfilling `requested_by` on reactor-dispatched runs (§3.3). Real, but it is a *notification* bug, and Option A routes around it rather than depending on it. Noted here so it is not lost.
- Removing Remind (#989 owns it). Labelling the SAVED popover (#988 owns it).
- `VERSION`, `pyproject.toml` version, `CHANGELOG.md`. Untouched.

---

## 8. Verification plan

The risk is that a control claiming to answer a gate actually advances the workflow, so the tests must prove the *run moved*, not that a request returned 200.

**Unit**

- `reply_run` on a non-waiting run → `RunNotWaiting`; unknown id → `UnknownRun` (mirrors the existing trio's tests).
- `reply_run` spawns the CLI with the text as `--event-data`, and does **not** take the claim itself (run_actions.py:77-79's rule).
- `workflows resume --event-data` parses to a dict and reaches `resume_workflow(event=...)`; malformed JSON exits non-zero without claiming.
- `_workflow_status` / `detail.resumable`: a run suspended 1 minute ago is `idle` **and** `resumable: true`. This is the §3.5 regression guard.

**Integration (isolated `BOBI_HOME`, real run records)**

- Suspend a two-step workflow on `await: approval`; POST the reply; assert the run reaches `completed` and that the post-await step observed `${{ event.text }}` equal to the typed note. This is the test that would fail today, and the one that proves the feature.
- Same, on a run with `requested_by == {}`, to pin §3.3: the gate passes with no Slack configured at all.
- Concurrent reply + close: exactly one wins, the other reports honestly (the `claim()` / `WorkflowRun.close` arbitration, run_actions.py:133-136).

**Brain**: not required. This is control-plane and read-model work with no LLM decision in the path, which is the brain-agnostic case CLAUDE.md's "one mechanism, two brains" rule exempts. The stub e2e proves it.

**Frontend** (`docs/FRONTEND_QA.md`): drive the real page against a seeded run and capture a GIF of type → confirm → row leaves `awaiting_action`. Attached to the PR per the house proof-of-work rule.

---

## 9. Implementation plan

Ordered so each step is independently reviewable, tests first.

1. Land **after #989** to avoid the `rowActions()` collision. Rebase on it.
2. Tests from §8, red.
3. `cli.py` `--event-data`.
4. `run_actions.reply_run` + the three runtime bindings + the route.
5. `agent.js` / `app.css` gate block, gated on `detail.resumable`.
6. Docs (§7) and the §3.7 message correction.
7. Review gate, full suite, frontend capture, PR.

---

## 10. Risks

| Risk | Mitigation |
|---|---|
| Operator reads "reply" as "message", gets "workflow proceeds" | Label is **Approve and continue**; confirm names workflow + step and says later steps will run (§6). This is the whole of RUN_RESUME.md's objection. |
| Re-opens force-resume in the UI against a recorded decision | Surfaced, not buried (§3.6). Gate 1 decides; §5.5 is the decline branch. |
| Gate 1 picks Option D | Steps 1, 6 and the §3.7 fix still land; 2-5 are dropped. |
| #989 / #988 conflicts in `agent.js` | Slab-only (§5.4) keeps this out of `rowActions()` and `renderSaved()`. Merge after #989. |
| A gate whose workflow was uninstalled | Already handled upstream: `remind_run` raises `ActionFailed` (run_actions.py:112-114); resume exits non-zero (cli.py:2433-2435). Reply inherits it. |

---

## 11. Decisions reserved for Gate 1

1. **Should the agent page be able to pass a human approval gate at all?** Option A/D. This is the whole ticket; everything else is detail.
2. **Confirm the reframing.** The issue's (a)/(b) fork is retired on the evidence in §3.1-§3.3. If that reframing is wrong, say so before step 2 of §9.
3. **Gate on `detail.resumable`, not `status === "awaiting_action"`** (§3.5). This deviates from the issue's literal wording and is the largest single change to the feature's value.
4. **Slab-only, no row button** (§5.4).
5. **Note required or optional** (§6).
6. **The RUN_RESUME.md amendment** recording that the UI may pass a gate when the control is labelled as approval (§3.6).

No code will be written until these are answered.
