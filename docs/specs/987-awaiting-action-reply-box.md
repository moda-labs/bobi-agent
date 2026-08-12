# 987 - An inline reply box on the awaiting-action Details slab

Status: **awaiting Gate 1** (design approval). No code written.
Issue: [#987](https://github.com/moda-labs/bobi-agent/issues/987) · Linear MOD-372 (part of MOD-368)
Author: engineer agent, 2026-08-12 · **Revision 2, 2026-08-12** (verification pass, see §12.1)
Base: `main` @ `8441de2`

> **Revision 2 changed the design, not just the prose.** A verification pass refuted one causal claim (§3.3), found a payload bug that would have silently discarded the operator's note (§4.1), found a scope omission that fails CI (§7), downgraded an overclaim about A1 (§1, §5.1) and one about `docs/RUN_RESUME.md` (§3.6), and added the question this spec had never asked: whether the control should exist at all (§3.11, decision 1). Corrections are folded into the sections they affect and summarised in §12.1.

---

## 1. Summary

The issue asks for a reply box at the bottom of the opened slab so an operator can answer a workflow gate without leaving the agent page.
It also asks a design question first: does the typed text get parsed/mapped to the gate's `await_event`, or does it get posted into the Slack thread so existing reply-parsing handles it?

**Both halves of that question rest on things that do not exist.**

- There is no reply-parsing to hand off to. The only event-driven resume entry point, `try_resume_for_event`, has **no production caller** (§3.1). A Slack reply has never advanced a gate.
- There is nothing to parse. Every waiting run records exactly **one** `await_event`, and the page already displays it. All 9 gates waiting in this deployment right now await the same event, `approval`, at the same step of the same workflow (§3.2).

So the fork is not (a) parse vs (b) forward.
It is: **does the page get an approve action, or does it not?**
Post-#989 the page can *abandon* a gate (Close) but cannot *pass* one, and force-resume was deliberately taken out of the UI (§3.6).

I recommend **Option A: reply-and-continue** (§5.1). The typed text rides as the resumed run's `event` payload, which `resume_workflow` already accepts and injects (orchestrator.py:410), so the frozen engine needs no change.
It must be **labelled as approval, not as a reply**, because that is mechanically what it is, and `docs/RUN_RESUME.md` is owed an amendment saying so.
This is less novel than it sounds: the page's own Locked plan approved a resume control here (U6/U7, both `[x]`) before it was withdrawn, so Option A largely restores an approved design rather than inventing one (§3.7).

Two things the issue did not ask about, both of which change the feature more than anything it did ask:

- `awaiting_action` is a **24-hour-derived** status, so a box gated on it is invisible for the first day of every gate's life (§3.5). Gate it on `detail.resumable` instead.
- Resume is fire-and-forget with both streams discarded, so **every post-spawn failure currently reads as success** (§3.9). A control whose failure mode is a green checkmark is worse than no control. Surfacing that is in scope here.

Ship it as **A1 (approve) then A2 (the note)**: the endpoint that passes a gate already exists, so the first slice is a frontend change (§5.1).

**Stated plainly, because it is a substitution and not a delivery.**
The issue asks for a reply **box**. A1 has **no text input at all** - it is an approve button with a confirm.
A1 therefore delivers the *outcome* this spec argues the operator actually needs (the page can pass a gate) by a *different mechanism* than the one the issue requested (a place to type).
The typed text only exists in A2.
I am recommending that substitution on the evidence in §3.1-§3.3, not claiming the issue's own request is satisfied by A1. Gate 1 should approve or reject the substitution knowingly, which is why §11 asks about A1-alone separately from the note.

**And one question this spec does not answer.** Every gate this feature would serve belongs to a workflow that an approved plan is migrating away from, and the queue of them has never once been cleared (§3.11).
Whether the control should exist at all is therefore a real question, and it is **decision 1 in §11**, for Gate 1 rather than for me.

---

## 2. Baseline this is specced against

The issue body is stale in two places. Verified against `main` @ `8441de2`:

| Issue says | Actually |
|---|---|
| "gives the operator only **Remind** and **Close**" | Correct today (agent.js:619-635), but sibling **#989** (MOD-371) is removing Remind now. **Post-#989 the row offers only Close.** |
| "see the comment at `agent.js:241-243`" | `agent.js:241-243` is the telemetry tile loop and the header actions span. No chat comment. **The cited line numbers belong to a different file**: `plans/2026-07-31-single-agent-view.md:241-243`, the U5 item `"keep /messages untouched for chat compatibility"`. In code the same intent lives at **server.py:353-354** (`"/messages above is the chat view and stays exactly as it was"`) and agent.js:10-12 (`"Chat lives in Slack and the CLI"`). §3.4 traces where that path actually goes. |
| `rowActions()` "lines ~619-636" | `rowActions()` is agent.js:597-637; the `awaiting_action` branch is 619-635. |

Siblings on the same file, same base: **#988** (MOD-373) edits `renderSaved()` (agent.js:315-359), **#989** edits `rowActions()`/`remind()` (agent.js:597-658).
This work touches `openSlab()`/`renderTranscript()`/`renderRowDetails()` (agent.js:691-794) and re-enters `rowActions()` only if Gate 1 picks the row-button variant in §5.4.
**#989 is the merge-order dependency**: it deletes lines inside `rowActions()` that this feature reads around.

That the row is down to a single Close button is not a footnote. It is the motivation:

> After #989, the only thing the agent page can do to a human approval gate is kill it.

### 2.1 The GitHub issue is narrower than its own Linear ticket

Checked while moving the ticket to In Progress. The Linear mapping is sound (`MOD-371` == #989, `MOD-373` == #988, parent `MOD-368` "Single agent UI v2" is In Progress), but the titles do not match:

| | |
|---|---|
| **Linear MOD-372** | "Give users the ability to chat with a **running** agent" |
| **GitHub #987** | "Add an inline reply box to the **awaiting-action** Details slab" |

Those are two different capabilities, and the difference decides whether §3.4 is a dead end or half the ticket:

- **Chat with a running agent.** The session is live, so it is in `ACTIVE_STATUSES`, and `POST /api/agents/{name}/chat` → `service.ask` is exactly the right path. **It already works.** No new mechanism at all.
- **Answer a suspended gate.** The session is `waiting`, so `service.ask` refuses it (§3.4), and the only thing that moves the run is a resume.

This spec answers the GitHub framing, which is the dispatched work.
It may therefore be **under-scoped against the Linear intent**: if MOD-372 wants both halves, the running-agent half is the cheap one and is not covered here.
I am not widening scope on my own reading of a title. **Reserved as decision 11 in §11.**

---

## 3. What actually happens today

Every claim below was derived in a worktree off `main` @ `8441de2`. The greps are printed so they can be re-run.

### 3.1 A Slack reply does not, and never did, advance a gate

An earlier draft grepped only `resume_workflow|\.claim()` over `--include=*.py bobi/` and called the result exhaustive. It was exhaustive **of that grep**: it never searched `resume_run`, so it missed five real call sites, and `--include=*.py` hid the TypeScript half entirely.

The widened grep, printed so it can be re-run:

```
$ grep -rn "resume_workflow\|\.claim()\|resume_run" \
    --include=*.py --include=*.ts --include=*.js bobi/ event-server/
bobi/cli.py:2409:    from .workflow.orchestrator import resume_workflow
bobi/cli.py:2426:    if not run.claim():
bobi/cli.py:2439:    success = resume_workflow(run, wf, timeout=timeout)
bobi/workflow/orchestrator.py:176:    resume path. Before wiring one up: resume_workflow re-stamps the registry
bobi/workflow/orchestrator.py:190:    if not run.claim():
bobi/workflow/orchestrator.py:206:        target=resume_workflow,
bobi/workflow/orchestrator.py:291:    and asks for it here. ``resume_workflow`` deliberately never sets it.
bobi/workflow/orchestrator.py:372:def resume_workflow(
bobi/webapp/run_actions.py:67:def resume_run(root: Path, run_id: str) -> dict:
bobi/webapp/run_actions.py:71:    (``try_resume_for_event``): ``resume_workflow`` re-stamps the session
bobi/webapp/server.py:202:    def resume_workflow_run(name: str, run_id: str) -> JSONResponse:
bobi/webapp/server.py:205:        return JSONResponse(rt.resume_run(name, run_id))
bobi/webapp/runtime.py:356:    def resume_run(self, name: str, run_id: str) -> dict:
bobi/webapp/runtime.py:377:        workflow. Same error vocabulary as ``resume_run``, plus
bobi/webapp/runtime.py:386:        run's session cancelled. Same error vocabulary as ``resume_run``.
bobi/webapp/runtime.py:878:    def resume_run(self, name: str, run_id: str) -> dict:
bobi/webapp/runtime.py:879:        return self._run_action("resume_run", name, run_id)
bobi/webapp/event_bus.py:475:    def resume_run(self, name: str, run_id: str) -> dict:
bobi/webapp/event_bus.py:476:        return self._run_write(name, "resume_run", run_id)
bobi/supervisor/admin.py:61:                            "resume_run", "remind_run", "close_run"})
bobi/supervisor/admin.py:74:_RUN_ACTIONS = frozenset({"resume_run", "remind_run", "close_run"})
bobi/supervisor/snapshot.py:30:# run_details / resume_run / remind_run / close_run) and the additive `detail`
event-server/worker/src/fleet.ts:365:	"resume_run",
event-server/worker/test/fleet.spec.ts:185:		expect(isAdminCommand("resume_run")).toBe(true);
```

Classifying every hit:

| Hit | What it is |
|---|---|
| cli.py:2409, 2426, 2439 | **Live path.** `bobi agent <n> workflows resume <run_id>` (cli.py:2395-2444). Claims, then resumes. |
| orchestrator.py:190, 206 | Inside `try_resume_for_event` (orchestrator.py:165-213). **Dead**: see below. |
| orchestrator.py:176, 291, 372; run_actions.py:71; runtime.py:377, 386; snapshot.py:30 | Definition, docstrings and comments, not calls. |
| server.py:202, 205 | The HTTP route, which delegates to **`rt.resume_run`** (the runtime, not `run_actions` directly) at :205. |
| run_actions.py:67 | The shared implementation, which **spawns the cli.py command**. Not a second implementation of resume. |
| runtime.py:356 | The `TeamRuntime` contract (abstract), not a call. |
| runtime.py:878-879 | `LocalRuntime` → `_run_action("resume_run", ...)` → `getattr(run_actions, action)(root, run_id)`. **String-dispatched**, which is why the narrow grep missed it. |
| event_bus.py:475-476 | `EventBusRuntime` (hosted) → `_run_write` → publishes the command over the fleet bus. Also string-dispatched. |
| admin.py:61, 74 | The supervisor's allowlist and its run-action set; dispatched via `getattr` at admin.py:483. |
| fleet.ts:365 | The Worker's `ADMIN_COMMANDS` allowlist. **Outside `bobi/` and outside `*.py`** - invisible to the old grep, and load-bearing (§7). |
| fleet.spec.ts:185 | Test, not production. |

**None of the five newly-found sites changes §3.1's conclusion**, and that is the point of printing them: they are all the *operator-initiated* resume path (page or CLI → spawn), not an event-driven one. No inbound event reaches any of them. The conclusion below stands on a wider inventory than the one that first produced it.

`try_resume_for_event` is the only function that maps an inbound *event* to a waiting run, and it says of itself:

> orchestrator.py:175-177
> `No production caller today; the CLI `workflows resume` is the only live resume path.`

The shipped doc agrees:

> docs/WORKFLOW_ENGINE.md:395-399
> `Resume is **manual today**: the event-driven entry point try_resume_for_event(...) exists in bobi/workflow/orchestrator.py and is covered by tests, but nothing in the runtime calls it, so a run that suspends on await: stays waiting even once its awaited event arrives.`

What a Slack reply *does* travel through today, traced end to end:
Slack gateway → event server → drain loop (`bobi/events/drain.py`) → `EventReactor.process()` (reactor.py:172-216) → either **launch a new workflow** (`_dispatch` → `launch_agent`, reactor.py:227-281) or fall through to the manager session's inbox for the LLM to read.
`EventReactor` has no branch that looks at waiting runs. Nothing on that path calls `find_waiting`, `claim()`, or `resume_workflow`.

**So option (b) as the issue words it cannot be built, because the thing it delegates to is not there.**

### 3.2 There is nothing to parse

`await_event` is a single recorded string per run (state.py:37), rendered on the row (agent.js:511-513) and in the slab (agent.js:778).
The runs read model already ships it in `detail` (runs.py:220-224).

Live evidence from this deployment's own `state/workflow/runs/`.

**This is a dated snapshot of a live queue, not a constant.** It is quoted here twice on purpose, because it moved between the two readings and the movement is itself evidence (§3.11):

```
SNAPSHOT B - 2026-08-12 (current)
total run records: 10   status counts: {'waiting': 9, 'running': 1}

  run_id     workflow          await       run_key             requested_by   input keys
  11d31ce5   issue-lifecycle   'approval'  '987'               {}             [repo, run_key, task]
  2a6c8c9e   issue-lifecycle   'approval'  '958'               {}             [repo, run_key, task]
  409b4300   issue-lifecycle   'approval'  '933'               {}             [repo, run_key, task]
  46aecc34   issue-lifecycle   'approval'  'adhoc-60c70cd8'    {channel: C0BAEN48KQR, ...}  [repo, run_key, task]
  75881342   issue-lifecycle   'approval'  'adhoc-016d4503'    {}             [repo, run_key, task]
  7eec97d1   issue-lifecycle   'approval'  '1006'              {}             [repo, run_key, task]
  b0529f37   issue-lifecycle   'approval'  'adhoc-57133a39'    {channel: C0BAEN48KQR, ...}  [repo, run_key, task]
  e097927a   issue-lifecycle   'approval'  'adhoc-e0dc9a95'    {}             [repo, run_key, task]
  e5f2573a   issue-lifecycle   'approval'  'adhoc-55df2bbc'    {channel: C0BAEN48KQR, ...}  [repo, run_key, task]

SNAPSHOT A - 2026-08-12, earlier the same day
total run records: 9    status counts: {'waiting': 8, 'running': 1}
  same rows minus 11d31ce5.
```

`11d31ce5` is **this ticket's own spec run**, which suspended on the same gate while this document was being written. The queue gained one and lost none.

**All 9 also carry a non-empty `session_name`**, checked separately because §5.4's design conclusion turns on it:

```
$ python3 -c "...; print(len(w), all(r.get('session_name') for r in w))"
9 True
```

That is what sends `openSlab()` down the transcript branch rather than the details branch (§5.4).

Reproduce:

```bash
python3 - <<'PY'
import json, glob, collections
rows = [json.loads(open(p).read())
        for p in glob.glob('<run>/state/workflow/runs/*.json')]
print(collections.Counter(r.get("status") for r in rows))
for r in rows:
    if r.get("status") != "waiting": continue
    sc = r.get("variable_scopes") or {}
    rb = sc.get("requested_by") or {}
    print(r["run_id"], r["workflow_name"], repr(r.get("await_event")),
          repr(r.get("run_key")), rb.get("channel", "-"),
          sorted((sc.get("input") or {}).keys()))
PY
```

Nine waiting gates, one distinct `await_event` between them.
`agents/eng-team/workflows/issue-lifecycle.yaml:76-77` (`await: approval`) is the **only** `await:` gate in the repo:

```
$ grep -rn "await:" --include=*.yaml --include=*.yml .
agents/eng-team/workflows/issue-lifecycle.yaml:77:    await: approval
```

A classifier that picks between one option is not a classifier.
**Option (a)'s "parse/map to the event" problem does not exist either.** The run already names its event; the only thing the operator's text can be is a *payload*.

### 3.3 Six of the nine live gates have no Slack thread to post into

`_execute_notify_step` resolves its destination from the `requested_by` scope, and gives up when there is none:

```
orchestrator.py:1431-1436
    requester = ctx.scopes.get("requested_by") or {}
    channel = requester.get("channel", "")
    thread_ts = requester.get("thread_ts", "")
    if not channel:
        return _undeliverable("no Slack channel available")
```

**The count is what matters here, and it holds: 6 of the 9 waiting gates have no address for option (b) to post to** (§3.2, snapshot B; it was 5 of 8 in snapshot A).
For those runs `remind_run` also cannot work, which is consistent with #989's report that Remind does nothing.

#### Why they are empty, corrected

An earlier draft of this spec blamed `reactor._dispatch` for not passing `requested_by`. **That explanation is wrong and is retracted here** so it is not re-derived.
`_dispatch` does omit `requested_by`, but **no waiting run went through it**, so it caused none of these cases.

The proof is in the `input` scope, which is stamped unconditionally on every run:

```
orchestrator.py:327-329  (run_workflow)
    input_scope = {"task": task, "repo": repo, "run_key": run_key}
    if input_fields:
        input_scope.update(input_fields)
```

The reactor **always** passes a non-empty `input_fields`, seeded with three keys before the event's own fields are merged in:

```
reactor.py:241-246
        input_fields = {
            "event_type": event_type,
            "repo": repo,
            "pr_number": number,
        }
        input_fields.update(fields)
```

So a reactor-dispatched run necessarily carries `event_type` and `pr_number` in its input scope.
**All 9 waiting runs carry exactly the base three keys `[repo, run_key, task]`, with `event_type` absent** (§3.2, right-hand column). None of them is reactor-dispatched, `933` / `958` / `1006` included.

The correlation the old story rested on also fails on this spec's own table: 2 of the empty-`requested_by` runs are `adhoc-*` rather than issue numbers, and `adhoc-*` runs appear on **both** sides of the split. Issue-number vs adhoc does not predict the channel.

**The actual cause is launcher discipline, not a code path.**
`requested_by` reaches a run only from the optional `--requested-by` flag on the launch command:

```
cli.py:2977-2978
@click.option("--requested-by", "requested_by", default=None,
              help='JSON identity of requester, e.g. \'{"from":"Alice","channel":"C1"}\'')
```

It defaults to `None`, `_parse_requested_by` turns that into `{}` (cli.py:3031-3032), and `run_workflow` normalises `{}` to "no scope set" (orchestrator.py:294, 331-332).
Every waiting run here was launched through `subagents launch`. The three that carry a channel had a launcher that happened to hold a Slack thread and passed it; the six that do not had a launcher that omitted the flag. Nothing warns, and nothing fills it in later.

This matters for the design in one direction only: it is **more** evidence for Option A, because the missing address is not attributable to a single fixable dispatch path. It is a per-launch omission spread across launchers, so any design that depends on a Slack thread existing (option (b), §5.2) is depending on a field that is optional by construction.

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

This spec does not smuggle it back in. It states it.

**What the doc actually records, and what is only my inference.**
An earlier draft asserted that RUN_RESUME.md's objection "is to a bare Resume button, and the fix it demands is honest confirm language, not a permanent ban". That overstated the source, and is corrected here.

- **What the doc says (fact).** RUN_RESUME.md:3-5 states the removal and stops. It records **no rationale at all** - not why the button went, not what would bring it back. The one thing it does add is a carve-out: the endpoint "remains available to **the CLI/framework** for explicit force-continuation."
- **What I inferred, and from where (inference).** The "honest confirm language" reading was drawn from RUN_RESUME.md:36-39, which is about the *payload shape* - why the response names the workflow and awaited event - not about whether a UI may offer the action.
- **The plain text cuts the other way.** Naming the CLI and framework as the surfaces that keep the capability reads most naturally as a **deliberate exclusion of the UI**, not as a ban pending better copy. A reader with only the doc in front of them would conclude the page is not supposed to do this.

So: the doc does not forbid Option A in so many words, but neither does it license it, and the nearest thing to a stated intent points away from it.
Option A's answer to that objection is to name the control **Approve and continue** and carry the workflow + step + awaited event in the confirm - but that is this spec's proposal for satisfying an unstated rationale, **not a requirement the doc sets out**.
Whether that is good enough is a judgement about a recorded decision with no recorded reasoning, which is exactly the call Gate 1 exists to make. §5.5 is the do-nothing branch, and decisions 2 and 8 in §11 ask it directly.

Two doc-consistency items fall out, both fixed in the implementing PR:

1. `docs/RUN_RESUME.md:20-22` still calls resume "the runs table's one write action", which contradicts its own header note at lines 3-5. Stale since force-resume left the UI.
2. `docs/WORKFLOW_ENGINE.md:401` says the checklist plan "proposes removing the await/resume feature rather than repairing it". The plan's re-approval superseded that: `plans/2026-07-26-checklist-execution-model.md` is **Approved (re-approved 2026-07-29)** and its thesis is that **"the engine is frozen, not deleted"**. Await/resume is frozen, not slated for removal. That matters here: a frozen engine is an argument for keeping this change in the webapp layer, which Option A does.

### 3.7 The governing plan already approved a resume control on this page, and already removed chat from it

This ticket is **not plan-born** (the issue body references no `plans/` artifact, and the title's `[MOD-372]` prefix is a Linear id, not a plan slug), so this spec covers the whole ticket rather than a slice.
But `agent.js:1-2` names the plan that produced this page, and that plan is **Locked (design approved 2026-07-31)**.
Two of its recorded decisions bear directly on this ticket, and they point in opposite directions:

**For.** The approved design put a resume control on exactly this surface:

```
plans/2026-07-31-single-agent-view.md:248-251  (U6 - runs write action)
- [x] `POST .../workflows/runs/{run_id}/resume` (reuse CLI resume logic;
      single-winner claim semantics preserved; 409 when not resumable).

plans/2026-07-31-single-agent-view.md:262      (U7 - the page)
- [x] Resume button + confirm.
```

Both shipped, both are `[x]`. The button was later withdrawn from the UI (docs/RUN_RESUME.md:3-5) while the endpoint stayed.
So **Option A is closer to restoring the plan's own approved U6/U7 than to inventing a new capability**, which is the strongest argument available for it.

**Against.** The same plan removed the typing surface, with a stated reason:

```
plans/2026-07-31-single-agent-view.md:129-130
- *Chat column removed* - Slack/CLI are the chat surfaces; the page is for
  observing and recovering.
```

A reply box is a typing surface returning to a page whose approved plan took one away.

**How the tension resolves, and what Gate 1 must confirm.**
The plan's own words allow it: the page is for "observing and **recovering**", and passing a stuck gate is recovery, not conversation.
That is why §6 insists the control is a **gate block that approves**, not a chat composer: labelled *Approve and continue*, one textarea whose content is a note carried to the next step, no message history, no threading, no polling for replies.
Under that framing this restores U6/U7 and leaves the chat decision intact.

If Gate 1 reads it the other way, the correct mechanism is not a quiet divergence: per CLAUDE.md, "post-approval changes are dated amendments, never silent rewrites", so the implementing PR carries a dated amendment to `plans/2026-07-31-single-agent-view.md` recording that the page regained a gate-approval control and why.
**Reserved as decision 9 in §11.** This spec does not assume the answer.

### 3.8 The reminder text already promises what the system cannot do

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

### 3.9 Resume is fire-and-forget, so every post-spawn failure reads as success

This is the most dangerous fact in this document for the feature being proposed, and it was found by reviewing the spec rather than the issue.

```
bobi/webapp/run_actions.py:90-99
    cmd = [sys.executable, "-m", "bobi.cli", "agent",
           paths.agent_name_for_root(root), "workflows", "resume", run_id]
    subprocess.Popen(cmd, cwd=str(root), start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ...
    return {"ok": True, "accepted": True, "run_id": run_id,
            "workflow": run.workflow_name,
            "await_event": run.await_event}
```

The child's exit code is never observed and both its streams go to `/dev/null`.
Everything that can go wrong *after* the spawn therefore returns `accepted: true` to the operator:

| Failure | Where it exits | What the operator sees |
|---|---|---|
| run already claimed | cli.py:2426-2428 | success |
| workflow no longer installed | cli.py:2433-2435 | success |
| `resume_workflow` returns False | cli.py:2443-2444 | success |

There is a live way to hit the first one permanently. `WorkflowRun.list_runs` globs `*.json` (state.py:196), which also matches the intermediate file a torn claim leaves behind.

**The orphan is `<run_id>.claiming.json`, not `.resuming.json`.** The engine says so itself:

```
state.py:90-97  (claim(), "Known and NOT fixed here (D071)")
    the claim is still two file operations, and a crash between them leaves
    ``<run_id>.claiming.json`` holding the pre-claim status ``waiting`` with
    no ``<run_id>.json`` left. ``find_waiting`` globs ``*.json``, so it keeps
    matching that orphan while ``claim`` can never win again ... ``close`` has
    the same shape via ``<run_id>.closing.json``.
```

`claim()` renames `<run_id>.json` → `.claiming.json` (state.py:103-106) and only then writes `status="resuming"` into `.resuming.json` (state.py:104, 116). So `.resuming.json` never holds `waiting`; the file that does is `.claiming.json`.
Note `docs/WORKFLOW_ENGINE.md:408-412` describes the older, pre-`.claiming.json` shape and is **stale against the code** - worth correcting alongside the other doc fixes in §7.

The conclusion is unchanged and if anything broader: `detail.resumable` stays `true` for a run `claim()` can never win again, and `close_run` has the identical failure via `.closing.json`.
The page would offer **Approve and continue** on such a run forever, report success every time, and never move.

This is **pre-existing**, not introduced here. But today it sits behind a control no one can reach from the UI, and this feature makes it *the* way to pass a gate, on a page that polls every 4 seconds.
A control whose failure mode is a green checkmark is worse than no control, because the operator stops watching.

**So it is in scope for this ticket**, and cheaply: the page already re-polls. After submitting, watch the row; if it is still `waiting` after the threshold below, say so inline ("the resume did not start - check the agent log") rather than leaving a success on screen.

**The threshold, specified rather than left as "N".**
`pollRuns` runs on a 4-second interval (agent.js:884), so the unit is 4s.

> **N = 3 consecutive polls, about 12 seconds.**

Chosen against what the child actually has to do before the row can move: spawn `sys.executable -m bobi.cli` cold, import click and the orchestrator, then `claim()` (cli.py:2426), which is the atomic rename that flips the record off `waiting`.
A cold interpreter plus imports is the whole of that budget, and 12s is generous for it while still being short enough that an operator is looking at the screen when the message appears.

Two things this deliberately does **not** do:

- It does not wait for the workflow to *finish*. The run leaving `waiting` is the success signal, per RUN_RESUME.md's "`accepted`, not `resumed`" contract. A resumed run may take minutes.
- It does not retry or escalate. It reports. The operator decides.

N is a UI constant, not a protocol value, so it can be tuned without touching the endpoint.
This is a UI-side fix requiring no change to the endpoint's fire-and-forget contract, which `docs/RUN_RESUME.md:29-34` defends on purpose.

### 3.10 Non-findings, recorded so they are not re-litigated - and one reclassified

- **Double submit.** Two rapid clicks spawn two resume processes; `claim()` arbitrates and the loser exits immediately (run_actions.py:80-82). Harmless, but §6 still disables the control on submit rather than spending a process to prove a point.
- **Polling cost.** The gate block adds no new timer; `pollRuns` already runs at 4s (agent.js:884).

#### RECLASSIFIED: the three-runtime binding is not merely duplication, and `reply_run` does not fit the seam

An earlier draft filed this as a non-finding: "A2 binds `reply_run` in three places, which looks like DRY breakage but is the existing anti-drift seam (run_actions.py:8-12), correct as-is."
The seam is real, but the second half was wrong. **The seam is shaped for run actions that take `(root, run_id)` and nothing else**, and `reply_run` needs to carry a payload.

All three delegates are string-dispatched and pass exactly two positional arguments:

```
runtime.py:887-892   (LocalRuntime)
    def _run_action(self, action: str, name: str, run_id: str) -> dict:
        root = self._resolve(name)
        return getattr(run_actions, action)(root, run_id)

admin.py:481-483     (supervisor)
    target = _require_run_id(run_id)
    return getattr(run_actions, command)(self.project_root, target)

event_bus.py:484-489 (hosted)
    def _run_write(self, name: str, command: str, run_id: str) -> dict:
        result = self._view_command(fleet, instance, command, {"run_id": run_id}, ...)
```

`getattr(run_actions, command)(root, run_id)` has no slot for `text`, and the hosted path's wire args are the literal dict `{"run_id": run_id}`.
So a `reply_run(root, run_id, text)` **cannot be reached** through any of the three without changing the delegate itself: a `text` parameter on `_run_action` and `_run_write`, a `text` key in the published args, and a read of `args.get("text")` in `admin.py:256`'s `_run_action(command, args.get("run_id"))` call.

Consequences, all of which land on A2 and none on A1:

- The change is wider than "bind one more method three times". It edits the shared dispatch shape that `resume_run` / `remind_run` / `close_run` also travel.
- It needs input validation on the new field at the supervisor boundary, alongside the existing `_require_run_id`.
- It is a second, independent reason (with §7's `fleet.ts`) that A2 is not a mechanical addition.

This does not sink A2. It prices it correctly, and it is part of why §5.1 recommends shipping A1 first.

### 3.11 The population this feature serves: never serviced, and slated for migration

This spec has treated the frozen engine as a **constraint** (build in the webapp layer, change nothing in `bobi/workflow/`) without ever asking it as a **question**. The facts below are put here so Gate 1 can. They are presented without a recommendation; see decision 1 in §11.

**Every waiting gate is the same gate, and none has ever been passed.**

```
waiting: 9
workflows:         {'issue-lifecycle': 9}
await:             {'approval': 9}
suspended_at_step: {7: 9}          # issue-lifecycle.yaml:76-78, `await_approval`
oldest started_at: 2026-07-30T21:49:11
newest started_at: 2026-08-12T14:46:14
runs with a resumed_at:  []        # none, ever
```

- One workflow, one step, one event. The queue is 9 deep and **13 days** old at its oldest.
- **`resumed_at` is empty on all 9.** No run in this deployment has been resumed by any means - not from the CLI, which is the one live path (§3.1). The gate has a 24h `timeout`, and these are far past it.
- The queue grew by one while this spec was being written and drained by none (§3.2).

**And the only producer of these gates is being migrated away.**
`plans/2026-07-26-checklist-execution-model.md` is titled *"move eng-team off the workflow step machine"*, is **Approved (re-approved 2026-07-29)**, and names its targets explicitly:

```
plans/2026-07-26-checklist-execution-model.md:26-33
**The step machine is deprioritized, not deleted (2026-07-29, Zach).** ...
So eng-team - the only fleet consumer, and really only two substantive
workflows, `issue-lifecycle` (11 steps) and `pr-closed` (4) - moves to
checklists; ... and the engine is **frozen**
```

`issue-lifecycle` is the workflow that produces all 9 gates, and it is one of the two named for migration.
`agents/eng-team/workflows/issue-lifecycle.yaml:77` is also the **only** `await:` gate in the repo (§3.2).

**What follows from this is genuinely ambiguous, and I am not resolving it.**

- It can be read as *build it*: a queue nobody can clear is precisely a missing control, the migration is not done, and until it is, gates keep arriving with no way to pass them.
- It can be read as *do not*: adding an operator control to the surface of a subsystem being retired spends frontend, docs and test budget on a path with a scheduled end, and the same operator effort could go into the migration.
- It can be read as *neither yet*: the reason the queue is 9 deep may not be a missing button. Nobody has cleared it from the CLI either, which is available and documented.

That last point is the one I would want answered before building, and it is not answerable from the code.

## 4. The design question, answered

> Does the reply need to be parsed/mapped to the `await_event` (same handling Slack replies get today), or is this a shortcut that posts into the Slack thread and lets existing reply-parsing handle it?

**Neither, and the premise of each is false.**

| | Verdict | Why |
|---|---|---|
| (a) parse/map the text to the `await_event` in the webapp | **Not needed** | One recorded event per run (§3.2). Nothing to disambiguate. The text is a payload, not a selector. |
| (b) post to the Slack thread, let existing parsing handle it | **Not possible** | No such parsing exists (§3.1). And 6/9 live gates have no thread (§3.3). |

The real question, and the one Gate 1 should answer:

> **Should the agent page be able to pass a human approval gate, and if so, is a typed note the right shape for the payload?**

### 4.1 The payload envelope, and the trap in it

Concretely, the typed text would travel: reply box → `POST .../runs/{run_id}/reply` → `run_actions.reply_run` → spawn `bobi agent <name> workflows resume <run_id> --event-data <JSON>` → `resume_workflow(event=...)` → `ctx.set_scope(...)` (orchestrator.py:410) → readable by later steps as `${{ event.text }}`.
No parsing, no classification, no Slack round-trip.

**The envelope is not optional, and getting it wrong loses the note silently.** An earlier draft of this spec wrote the example as `--event-data '{"text": "..."}'` and described the injection as `ctx.set_scope("event", event["data"])`. Both were wrong, and together they hid the bug. The real line is:

```
orchestrator.py:410  (resume_workflow)
    if event:
        ctx.set_scope("event", event.get("data", {}))
```

`resume_workflow` unwraps a **`data` envelope**, and it does so with `.get(..., {})` rather than `event["data"]`.
That default is what makes the failure silent: an unwrapped payload does not raise `KeyError`, it sets the `event` scope to `{}`.
The operator's note vanishes, the workflow resumes anyway, `${{ event.text }}` renders empty, and **nothing anywhere reports a problem**.

> **Correct:** `--event-data '{"data": {"text": "the note"}}'`
> **Silently drops the note:** `--event-data '{"text": "the note"}'`

This is the same failure class §3.9 raises about resume itself - a success indication covering a no-op - reproduced one layer down, in this spec's own worked example. It is called out here rather than left to implementation because the wrong form looks correct and tests written from it can pass while the feature does nothing (see §8).

The envelope is the engine's existing convention, not something invented for this ticket:

```
orchestrator.py:869
    run = WorkflowRun.create(workflow.name, {"data": {"run_key": run_key}})
```

`try_resume_for_event` passes its `event` through to `resume_workflow` unchanged (orchestrator.py:205-210), so an event-driven resume would carry the same shape.

**Binding consequences** wherever this payload appears:

- `run_actions.reply_run` builds the envelope; the operator's raw text is never passed as the top-level object.
- `cli.py --event-data` takes the **already-enveloped** JSON, so the CLI stays a thin pass-through and the shape lives in one place.
- Any test that asserts only "`--event-data` parsed to a dict and reached `resume_workflow(event=...)`" would **pass on the broken form**. §8 requires asserting the resulting scope value instead.

---

## 5. Options

### 5.1 Option A - reply-and-continue (RECOMMENDED), shipped as two slices

The box resumes the run and carries the text as the resumed run's `event` payload.

- **Works for all 9 live gates**, including the 6 with no Slack thread.
- **Zero engine change.** `resume_workflow` already takes `event` and injects it, unwrapping a `data` envelope (orchestrator.py:410, §4.1). The engine is frozen (§3.6); this respects that.
- **Cost, stated plainly**: it re-opens a UI affordance RUN_RESUME.md closed, and that doc's only stated intent points away from a UI surface (§3.6). Mitigated by naming and confirm copy (§6), not by pretending otherwise.
- Risk: an operator reads "reply" as "send a message" and gets "workflow proceeds". **The label must say approve.** See §6.

**Ship it in two slices, because the headline outcome needs no backend at all.**
The endpoint that passes a gate already exists and is already wired end to end:

```
bobi/webapp/server.py:201-205
    @app.post("/api/agents/{name}/workflows/runs/{run_id}/resume")
    def resume_workflow_run(name: str, run_id: str) -> JSONResponse:
        if not safe_name(run_id):
            return JSONResponse({"error": "unknown run"}, status_code=404)
        return JSONResponse(rt.resume_run(name, run_id))
```

`resume_run` is bound on every runtime already: `TeamRuntime` contract at runtime.py:356, **`LocalRuntime`** at runtime.py:878, **`EventBusRuntime` (the hosted one)** at event_bus.py:475, and the supervisor at admin.py:61,74. (An earlier draft labelled runtime.py:878 "hosted"; it is `LocalRuntime` (runtime.py:551), and `event_bus.py` is the hosted path.)

| Slice | What it delivers | Files |
|---|---|---|
| **A1 - approve** | The gate block in the slab, calling the **existing** resume endpoint. The page can pass a gate. **No text input.** | `agent.js`, `app.css`, `runs.py` (one additive read-model field, §6.2), the §3.8 string, tests, docs. **No CLI, no new API surface.** |
| **A2 - the note** | The typed text carried into the resumed run as `${{ event.text }}`, enveloped per §4.1. **This is the slice that adds the box the issue asked for.** | `+ run_actions.py`, `server.py`, `runtime.py`, `event_bus.py`, `admin.py`, `cli.py`, **`fleet.ts`** |

**What A1 does and does not deliver, stated exactly.**
A1 delivers the *outcome* argued for throughout §3: the page gains a way to pass a gate, which it does not have today.
It does **not** deliver the *mechanism* the issue asked for. The issue asks for a reply **box**; A1 has no text input, and the operator types nothing. The box arrives only in A2.
Calling A1 "the whole stated goal" would be an overclaim, and §1 now names the substitution rather than performing it. Gate 1 is being asked to accept a different mechanism for the stated need, which is decision 3 in §11.

A1 is **five code files** and no new API surface (an earlier draft said three; it had missed the read-model line §6.2 needs, and the §3.8 string).
**A2 is seven more code files**, and it crosses a language boundary. Two of them were missed by an earlier draft and are the subject of §7's CI note and §3.10's reclassified finding:

- `event-server/worker/src/fleet.ts` - the `ADMIN_COMMANDS` allowlist. Adding `reply_run` to only the Python half **fails `tests/test_admin_command_parity.py` outright**; adding it to neither 400s the hosted path.
- the three run-action delegates, which are shaped `(root, run_id)` and have no slot for a payload (§3.10). Threading `text` edits the dispatch shared with `resume_run` / `remind_run` / `close_run`.

Why this split is the recommendation and not a nicety:

- It takes the outcome from a twelve-file, two-language change to a five-file, one-language one, which is the difference between a reviewable diff and an architectural one.
- It lets Gate 1 approve the **capability** (may the page pass a gate?) without simultaneously ratifying a **payload design** (what does a note mean to the next step?). Those are separate questions and §11 asks them separately.
- It nearly removes the #989 collision: A1 touches `openSlab()` only.
- If Gate 1 says no to the note but yes to approval, A1 still ships whole.

The honest counter-argument, recorded: **A1 alone answers the issue's title but not its text.** If Gate 1 considers the typed note the point of the ticket rather than a refinement of it, A1-alone is the wrong first slice and A1+A2 should land together.

**Reserved as decision 10 in §11.** If Gate 1 wants the note in v1, A1+A2 land together and nothing here is wasted.

### 5.2 Option B - post into the gate's Slack thread

Reuse `requested_by` + `post_slack_message` the way `remind_run` does.

- Delivers a visible message. **Does not advance the gate.**
- **Fails outright for 6/9 live gates** (§3.3) with `no Slack channel available`, and the missing field is a per-launch omission rather than one fixable dispatch path.
- Makes §3.8's false promise worse: the operator now types into Bobi, sees it land in Slack, and still nothing happens.
- Rejected. It is a strictly more expensive way to reach today's outcome.

### 5.3 Option C - reuse the `/chat` plumbing

The issue's explicit pointer. **Provably dead**: `ask()` rejects the suspended session (§3.4). Rejected on mechanism, not on taste.

### 5.4 Sub-decision inside Option A: where the control lives

The issue says "bottom of the opened slab". Two wrinkles:

1. **All 9 live waiting runs carry a `session_name`** (§3.2, re-verified on snapshot B), and `runs.py:215` sets `session_id=run.session_name`. So `openSlab()` takes the **transcript** branch (agent.js:700-707), not `renderRowDetails` (agent.js:766-782). A box written only into the Details renderer would never appear on a real gate. It must attach after **either** renderer, in `openSlab()`, keyed on `detail.resumable`.
2. The slab opens on row click **and** on the Transcript/Details buttons (agent.js:545-551, 603-616), so the box is reachable either way. No row-button variant is required. Recommending slab-only keeps `rowActions()` untouched and removes the #989 merge collision.

**Recommendation: slab-only, appended in `openSlab()` after the renderer returns.**

### 5.5 Option D - decline, and fix only the false promise

If Gate 1 holds that the page must not pass gates: keep Close as the only write, and still land §3.6's two doc fixes plus §3.8's message correction.
Small, honest, and leaves the operator with Slack or the CLI. Named so the decision is a real choice.

---

## 6. UX, per the design system

`docs/design-system/README.md:369-372` is binding and non-negotiable here:

> **Gates are sacred.** A human approval step always renders with the violet left rail + 4% wash + rotated-square glyph, always names the workflow and step that stopped, and is never downgraded to a toast or an auto-dismissing dialog.

The block is therefore a **gate**, not a chat composer:

- Violet left rail, 4% wash, rotated-square glyph. Violet is state, never decoration.
- Names the workflow, the step that stopped, and the awaited event. **The step is the problem here; see §6.2.** The workflow (`row.title`) and awaited event (`detail.await_event`) are already in the row payload (runs.py:220-224).
- Label: **Approve and continue** (primary), with the textarea labelled as a note carried to the next step. Not "Send". Not "Reply".
- Confirm before firing, in RUN_RESUME.md:36-39's terms: name the workflow and step, and say that continuing proceeds *as if approved* and that later steps will run.
- Failure renders inline in the slab (the `slabError`/`showReport` idiom, agent.js:291-296, 724-727). Never a toast.
- The control **disables on submit** and re-enables on a terminal outcome, following the `remind()`/`closeRun()` idiom already in the file (agent.js:640-641, 667-668).
- **Accepted is not done.** `accepted: true` means a process was spawned, not that the gate passed (§3.9). Render a pending state, keep watching the row, and if it is still `waiting` after **3 polls (~12s, §3.9)** say the resume did not start. Never leave an unqualified success on screen.
- Chrome is lowercase; mono for the run id / event name (data), sans for prose.
- The 409 case is a normal outcome, not an error shout: the run moved, so re-poll and re-render. (`_waiting_run` raises `RunNotWaiting` at run_actions.py:63; the literal "no longer waiting" string is `close_run`'s `ActionFailed` at run_actions.py:136. Two different refusals, both surfacing as 409-class.)

### 6.1 The CSS is not a reskin of something that exists

An earlier draft scoped this as "gate block styling from existing tokens", which understated it twice.

**There is no gate component to extend.** `app.css` has no gate primitive at all:

```
$ grep -n "gate\|rail" bobi/webapp/static/app.css
378:   roster: health (manager liveness + lifecycle t[rail] ...) and spend. One shared
540:/* Selection gets a violet rail, not a colored outline. */
```

One comment about row selection, one incidental substring match inside "trail", and **no `gate` hit at all**. The violet left rail, the 4% wash and the rotated-square glyph the design system mandates would all be **new components**, authored against `tokens.css`, not variations on an existing block.

**And violet already means something else on this page.** All three of its current uses are liveness:

```
$ grep -n "violet" bobi/webapp/static/app.css
109:/* connection health. Connected is LIVE (violet); a stale link is waiting
315:/* live / in-flight - the only states that earn violet */
540:/* Selection gets a violet rail, not a colored outline. */
```

app.css:315 is explicit: *"live / in-flight - the only states that earn violet"*.
A waiting gate is the opposite of in-flight. So a violet gate block puts violet on a **stopped** run, on a page where violet has so far meant **running**, and the two would sit in the same table.

Both readings are sanctioned by the design system, which lists "live, enforced, **gated**, focused" as violet's meanings. The conflict is local to this page, not with the system.
It still has to be resolved deliberately, and the resolution belongs to the design system's own vocabulary rather than to a new colour:

- The gate block is distinguished from the live indicators by **form**, not hue: the rotated-square glyph and the rail plus wash are structural, and no live indicator on this page uses either.
- The live states stay dot/pill-shaped as they are today. Nothing existing gets restyled.

This is a real design task with a real chance of an ugly result in the middle of a dense table, and it should be reviewed on a screenshot rather than approved from prose. Flagged so the A1 estimate is not read as "add a class".

### 6.2 The gate cannot name the step that stopped, and A1 is not "no backend" because of it

The design system's rule 2 is binding and specific:

> `docs/design-system/README.md:369-372` - a human approval step "always **names the workflow and step that stopped**".

The page cannot currently satisfy that. An earlier draft asserted the confirm could be built from `detail.suspended_at_step` with "no new fetch". Both halves of that are wrong.

**1. `suspended_at_step` is the NEXT step, not the one that stopped.**

```
orchestrator.py:869-871
    run = WorkflowRun.create(workflow.name, {"data": {"run_key": run_key}})
    run.status = "waiting"
    run.suspended_at_step = step_idx + 1
```

Confirmed by the shipped doc: `docs/WORKFLOW_ENGINE.md:389-390` calls it "`suspended_at_step` (the index of the *next* step)".
On the live gates this is not academic. All 9 record `suspended_at_step: 7`, and in `issue-lifecycle.yaml` **index 6 is `await_approval` (the gate) while index 7 is `implement`**.
A confirm built naively from that field would tell the operator it is approving `implement`, which is the step about to *run*. On a control whose entire safety argument is that the confirm says what is being waved through, that is the worst possible off-by-one.

**2. There is no step name in the payload to use anyway.** `detail.suspended_at_step` is a bare integer, and `WorkflowRun` has no name field:

```
state.py:29-43  (WorkflowRun)
    suspended_at_step: int = -1
    await_event: str = ""
    ...          # no step name, at any index
```

The workflow-run branch of the read model ships the integer and nothing else (runs.py:220-224). The step **name** is recorded at suspend, but only onto the session registry:

```
orchestrator.py:867
    registry.update(session_name, status="waiting", phase=step.name)
```

and `runs.py` folds `phase` into `detail` only on the *session* branch (runs.py:169), not the workflow-run branch.

**Consequence: A1 needs a read-model change, so "no backend, no new API surface" is not accurate.**
The cheapest correct fix is to widen the workflow-run `detail` in `bobi/webapp/runs.py` to carry the stopped step's **name** - resolved from the session registry's `phase`, which already holds exactly it. That is a few lines in one file, additive, no engine change and no new endpoint, but it **is** backend and it belongs in A1's scope rather than being discovered during implementation.

Three alternatives, rejected:

- *Subtract one and show the index.* Still a bare number, still does not "name" the step, and it hard-codes an engine invariant into the frontend.
- *Resolve the workflow YAML client-side.* The page has no workflow-definition endpoint, and adding one is far more than the read-model line.
- *Store the step name on `WorkflowRun`.* It is the tidiest long-term shape, but it edits `bobi/workflow/state.py` - the frozen engine (§3.6) - and would not backfill the 9 runs already waiting. Recorded as the right fix if the freeze ever lifts.

**This also makes §8's confirm-string test load-bearing rather than pedantic.** That test now has a specific job: assert the confirm names `await_approval`, and would fail against the `implement` string the naive implementation produces.

Open UX question for Gate 1: whether the note should be **required** or optional (A2 only; A1 has no note at all). Required makes the operator state a reason and produces a better audit trail; optional makes the common "yes, go" case one click. I lean optional with a placeholder that says the note reaches the next step.

---

## 7. Scope

**In - A1 (approve). One additive read-model line; no CLI, no new API surface, no engine change.**

- `bobi/webapp/runs.py`: widen the workflow-run `detail` to carry the **name** of the step that stopped (from the session registry's `phase`). Required by the design system's "names the workflow and step that stopped", and not currently derivable on the page - `suspended_at_step` is the *next* step's index and there is no name anywhere in the payload (§6.2). Additive; no existing field changes shape.

- `bobi/webapp/static/views/agent.js`: gate block appended in `openSlab()` after either renderer, gated on `detail.resumable`; confirm, submit against the **existing** `POST .../runs/{run_id}/resume`, disable-on-submit, inline error, and the accepted-is-not-done watch (§3.9).
- `bobi/webapp/static/app.css`: the gate block. **New components, not a reskin** - there is no existing gate primitive, and violet's current meaning on this page needs resolving (§6.1).
- `tests/`: the Node-executed JS tests in §8, including the §3.5 regression guard.
- Docs: `docs/RUN_RESUME.md` (the page may pass a gate again + the §3.6 item 1 correction), `docs/RUN_DRILLDOWNS.md` (the slab now has a write surface), `docs/WORKFLOW_ENGINE.md:401` (§3.6 item 2), **`docs/WORKFLOW_ENGINE.md:408-412`** (it describes the torn-claim orphan as `.resuming.json`; the code says `.claiming.json`, §3.9), `docs/RUNS_VIEW.md:55` (row actions after #989).
- `bobi/workflow/orchestrator.py:107-112`: correct the reminder's false promise (§3.8). **This is a string in the frozen engine, not a behaviour change** - the only edit outside the webapp layer, and it lands whichever way Gate 1 decides (§5.5).

So A1 is **five files** (`agent.js`, `app.css`, `runs.py`, `orchestrator.py` string, tests) plus docs, not the "roughly three" an earlier draft claimed. It remains a frontend-shaped change with no new endpoint and no new API surface; it is no longer literally "no backend".

**In - A2 (the note), only if Gate 1 takes decision 10 that way.**

- `bobi/webapp/run_actions.py`: `reply_run(root, run_id, text)`, sharing `_waiting_run` with the other three, and building the `{"data": {...}}` envelope (§4.1).
- `bobi/webapp/server.py`: `POST /api/agents/{name}/workflows/runs/{run_id}/reply`.
- `bobi/webapp/runtime.py` (`TeamRuntime` + `LocalRuntime`), `bobi/webapp/event_bus.py`, `bobi/supervisor/admin.py`: `reply_run` alongside the existing trio, so local and hosted stay one implementation (run_actions.py:8-12). **Each also needs its run-action delegate widened to carry `text`** - `_run_action` and `_run_write` pass `(root, run_id)` only, so `reply_run` is unreachable through them as they stand (§3.10).
- **`event-server/worker/src/fleet.ts`: add `reply_run` to `ADMIN_COMMANDS`.** Not optional and not cosmetic:

  ```
  tests/test_admin_command_parity.py:46-51  (in test_the_two_halves_..., 42-57)
      unreachable = python - worker
      assert not unreachable, (
          "these commands are implemented by the supervisor but NOT allowed by "
          "the server, so they 400 before reaching any deployment: "
          f"{sorted(unreachable)}. Add them to ADMIN_COMMANDS in "
          f"{FLEET_TS.name}.")
  ```

  The test asserts the two `ADMIN_COMMANDS` sets are equal **in both directions** (`dropped = worker - python` at :53-57 is the other half). Adding `reply_run` to `bobi/supervisor/admin.py:55-61` alone turns CI red; adding it to neither leaves the hosted reply path rejected with 400 "unknown command" by `index.ts` while every Python test still passes. That silent-in-production asymmetry is the exact failure the test was written for.
- `bobi/cli.py`: `--event-data` on `workflows resume`, taking the already-enveloped JSON (§4.1).
- The integration tests in §8 that assert `${{ event.text }}` reached the post-await step.

**Out**

- Wiring `try_resume_for_event`. Its own docstring (orchestrator.py:175-182) says a thread-based resume would stamp the wrong pid, and the engine is frozen. Not this ticket.
- Any approve/**reject** vocabulary in the engine. The engine has no reject (§3.6); adding one is an engine change against a frozen engine.
- **Making `requested_by` reliable at launch (§3.3).** Restated, because an earlier draft scoped this as "backfilling `requested_by` on reactor-dispatched runs" - a path **no waiting run took**, so that item pointed at nothing. The real gap is that `--requested-by` is an optional flag every launcher may omit, which is a launch-time notification bug affecting `remind_run` and the notify steps. Still out of scope: Option A routes around it rather than depending on it. Kept so it is not lost, now aimed at the right thing.
- Removing Remind (#989 owns it). Labelling the SAVED popover (#988 owns it).
- `VERSION`, `pyproject.toml` version, `CHANGELOG.md`. Untouched.

---

## 8. Verification plan

The risk is that a control claiming to answer a gate actually advances the workflow, so the tests must prove the *run moved*, not that a request returned 200.

**Unit**

- `reply_run` on a non-waiting run → `RunNotWaiting`; unknown id → `UnknownRun` (mirrors the existing trio's tests).
- `reply_run` spawns the CLI with the note **wrapped as `{"data": {"text": ...}}`**, and does **not** take the claim itself (run_actions.py:77-79's rule).
- **The envelope assertion, and why it is worded this way.** `workflows resume --event-data` must be tested by asserting the **resulting scope value** - that after `resume_workflow`, `ctx.scopes["event"] == {"text": "the note"}` - not by asserting that a dict reached `resume_workflow(event=...)`.
  A test written the second way **passes on the broken form**: `event.get("data", {})` accepts `{"text": ...}` without error and silently yields `{}` (§4.1). The call was made, the argument was a dict, the note is gone.
  Add the negative directly: an **unenveloped** payload must be caught. Whether that is a `reply_run`-side rejection or a CLI-side one is an implementation choice, but it must not resume-and-drop.
- `workflows resume --event-data` with malformed JSON exits non-zero without claiming.
- `_workflow_status` / `detail.resumable`: a run suspended 1 minute ago is `idle` **and** `resumable: true`. This is the §3.5 regression guard.
- **`tests/test_admin_command_parity.py` passes with `reply_run` present in both `ADMIN_COMMANDS` sets** (§7). This test already exists and needs no authoring; it is listed because it is the gate that fails if `fleet.ts` is forgotten, and because `test_the_parse_is_not_vacuous` asserts `len(worker) >= 15` on a list that currently holds exactly 15.

**Integration (isolated `BOBI_HOME`, real run records)**

- Suspend a two-step workflow on `await: approval`; POST the reply; assert the run reaches `completed` and that the post-await step observed `${{ event.text }}` equal to the typed note. This is the test that would fail today, and the one that proves the feature.
- Same, on a run with `requested_by == {}`, to pin §3.3: the gate passes with no Slack configured at all.
- Concurrent reply + close: exactly one wins, the other reports honestly (the `claim()` / `WorkflowRun.close` arbitration, run_actions.py:133-136).

**Brain**: not required. This is control-plane and read-model work with no LLM decision in the path, which is the brain-agnostic case CLAUDE.md's "one mechanism, two brains" rule exempts. The stub e2e proves it.

**Frontend, as a real test and not only a screenshot.**
The highest-value behaviour in this spec is a frontend predicate (`detail.resumable`, not the 24h status), and the first draft of this plan proposed proving it with a GIF. A GIF is proof-of-work, not a regression test, and the 24h bug is exactly the kind that silently comes back.

The repo already has the harness for this and it is not Playwright:

```
tests/test_webapp_markdown.py:1-11
"""The webapp's agent-reply markdown renderer, executed as real JavaScript.
 ... Asserting on the JS source text would prove nothing about what it renders,
 so these run the actual module under Node and parse what comes back.
 Node is already a hard requirement of this repo's build ..."""
```

Follow that pattern:

- Gate block renders when `detail.resumable` is `true` and `status` is `idle` (the pre-24h case). **This is the §3.5 regression guard, and it is the one test that must exist.**
- Gate block does not render when `detail.resumable` is `false`.
- The confirm string names the workflow, the step, and the awaited event. The entire safety argument of §3.6/§6 rests on that copy being honest; an untested string drifts.
- Submitting disables the control (§3.10).

**Failure surfacing (§3.9)**: after a submit that the backend accepted, a row still `waiting` after 3 polls (~12s) renders the inline "did not start" state rather than leaving success on screen. The test drives the poll clock rather than sleeping.

**Manual capture, still required.** Drive the real page against a seeded run and attach a GIF of type → confirm → row leaves the gate, per the house proof-of-work rule. `plans/2026-07-31-single-agent-view.md` U8 already ships a seed script that populates "completed + stalled workflow runs", so the fixture exists.

---

## 9. Implementation plan

Ordered so each step is independently reviewable, tests first.

**A1 - approve**

1. Land **after #989** to avoid the `rowActions()` collision. Rebase on it.
2. Node-executed JS tests from §8, red. The §3.5 regression guard first.
3. `agent.js` / `app.css` gate block, gated on `detail.resumable`, against the existing resume endpoint. Includes disable-on-submit and the accepted-is-not-done watch (§3.9).
4. Docs (§7) and the §3.8 message correction.
5. Review gate, full suite, frontend capture, PR.

**A2 - the note** (only under decision 10)

6. `cli.py` `--event-data`, with the malformed-JSON test **and the envelope-value assertion from §8** (assert the resulting `event` scope, not the call).
7. `run_actions.reply_run` building the `{"data": {...}}` envelope, + the route.
8. Widen the three run-action delegates to carry `text` (`runtime._run_action`, `event_bus._run_write`, `admin._run_action`), then bind `reply_run` on each (§3.10).
9. **Add `reply_run` to `ADMIN_COMMANDS` in BOTH `bobi/supervisor/admin.py` and `event-server/worker/src/fleet.ts`, in the same commit.** `tests/test_admin_command_parity.py` fails on either half alone (§7). Doing this as one step is the point: split across commits, the branch is red in between for a reason that reads like an unrelated failure.
10. Point the gate block at `/reply` instead of `/resume`; add the `${{ event.text }}` integration tests.

If Gate 1 takes A1+A2 together, this is one PR in this order rather than two.

---

## 10. Risks

| Risk | Mitigation |
|---|---|
| Operator reads "reply" as "message", gets "workflow proceeds" | Label is **Approve and continue**; confirm names workflow + step and says later steps will run (§6). Note this addresses an objection RUN_RESUME.md never actually states - the doc gives no rationale (§3.6). |
| **Operator approves, sees success, nothing happens** (§3.9) | The highest-severity risk here, and pre-existing. `accepted: true` only means a process spawned. The page watches the row for 3 polls and says "the resume did not start" rather than leaving success on screen. Tested in §8. |
| **The note is silently discarded** (§4.1) | An unenveloped `--event-data` payload sets the `event` scope to `{}` with no error: the gate passes, `${{ event.text }}` is empty, nothing reports it. Mitigated by building the envelope in one place (`reply_run`) and by §8's rule that tests assert the resulting scope value, never just the call. **A2 only.** |
| A torn claim makes a dead run look actionable forever (§3.9) | Same mitigation: the watch turns a silent no-op into a visible one. The underlying D071 window is recorded in WORKFLOW_ENGINE.md and is not this ticket's to close. |
| Re-opens force-resume in the UI against a recorded decision | Surfaced, not buried (§3.6), and the decision's *reasoning* was never written down, so it cannot be honoured precisely. Gate 1 decides; §5.5 is the decline branch. |
| **`reply_run` in only one `ADMIN_COMMANDS` half** | Red CI (Python-only) or a 400 in production with green tests (TypeScript-only). Both halves land in one commit, step 9 of §9. |
| Gate 1 picks Option D | The §3.8 reminder-string fix and the doc corrections land (§5.5); everything else is dropped. Note the §7 doc list assumes the control ships, so under Option D the RUN_RESUME/RUN_DRILLDOWNS entries are replaced by the §3.6 staleness fixes rather than written as-described. |
| **Gate 1 answers decision 1 "do not build"** (§3.11) | Nothing here is wasted: §3.1-§3.4 and §3.8's false-promise fix stand on their own as recorded findings, and §5.5 is a real, small deliverable. |
| #989 / #988 conflicts in `agent.js` | Slab-only (§5.4) keeps this out of `rowActions()` and `renderSaved()`. Merge after #989. |
| A gate whose workflow was uninstalled | Already handled upstream: `remind_run` raises `ActionFailed` (run_actions.py:112-114); resume exits non-zero (cli.py:2433-2435). Reply inherits it. |

---

## 11. Decisions reserved for Gate 1

1. **Should this gate control be built at all, given what it would serve?** (§3.11). Put first because it is prior to every other decision here, and because this spec does **not** answer it.
   The facts, neutrally: all 9 waiting gates are the same step of the same workflow; **none has ever been resumed by any means**, including the CLI path that already works; the oldest has waited 13 days; and `issue-lifecycle` is one of the two workflows the Approved `plans/2026-07-26-checklist-execution-model.md` is migrating off the step machine.
   The spec has used that freeze as a *constraint* (build in the webapp layer, touch no engine code) but never asked it as a *question*.
   Three readings are all defensible, and choosing between them needs context I do not have: **(a) build it** - a queue nobody can clear is a missing control, and gates keep arriving until the migration is done; **(b) do not** - this spends frontend, docs and design budget on the surface of a subsystem with a scheduled end; **(c) diagnose first** - nobody has cleared these from the CLI either, so the missing button may not be why the queue is 9 deep.
   If the answer is (b) or (c), stop here: decisions 2-10 do not arise.
2. **Should the agent page be able to pass a human approval gate at all?** Option A/D. Given a yes to 1, this is the whole ticket; everything else is detail.
3. **Accept A1's substitution?** (§1, §5.1). The issue asks for a reply **box**; A1 ships an approve button with **no text input**, and the box arrives only in A2. A1 delivers the outcome this spec argues for, not the mechanism the issue requested. That trade should be taken knowingly.
4. **Confirm the reframing.** The issue's (a)/(b) fork is retired on the evidence in §3.1-§3.3. If that reframing is wrong, say so before step 2 of §9.
5. **Gate on `detail.resumable`, not `status === "awaiting_action"`** (§3.5). This deviates from the issue's literal wording and is the largest single change to the feature's value.
6. **Slab-only, no row button** (§5.4).
7. **Note required or optional** (§6). A2 only.
8. **The RUN_RESUME.md amendment** recording that the UI may pass a gate when the control is labelled as approval (§3.6).
   Weigh this knowing the doc records **no rationale** for having removed force-resume - only the removal, plus a carve-out keeping the endpoint "available to the CLI/framework". This spec's "honest confirm language" reading is an inference from a passage about payload shape, and the plain text arguably points the other way. You may be the only person who knows what the original reasoning was.
9. **Does restoring a gate control need a dated amendment to the Locked plan?** (§3.7). The plan approved a resume button on this page (U6/U7) *and* removed the chat column with the reason "the page is for observing and recovering". My reading is that an approval control is recovery and needs no amendment; if you disagree, the implementing PR carries a dated amendment rather than diverging silently.
10. **Ship A1 alone first, or A1+A2 together?** (§5.1). A1 is the approve control on the existing endpoint: five files, no new API surface. A2 adds the note payload across **seven** more files spanning Python, TypeScript and the CLI, and requires widening the shared run-action delegates (§3.10) and both `ADMIN_COMMANDS` halves (§7). I lean A1 first, so "may the page pass a gate?" is answerable without also settling "what does a note mean to the next step?" - but see decision 3: A1 alone answers the issue's title and not its text.
11. **Scope: does MOD-372 want the running-agent half too?** (§2.1). The Linear ticket says "chat with a **running** agent"; the GitHub issue says "reply box on the **awaiting-action** slab". This spec covers only the second. The first is cheap, because `POST /api/agents/{name}/chat` → `service.ask` already works for a live session - it is only the *suspended* case that has no path (§3.4). I am not widening scope on my own reading of a title.

No code will be written until these are answered.

---

## 12. Review record

Reviewed under the house spec gate before publication. Not plan-born (§3.7), so the scope lens applied.

| Lens | Outcome |
|---|---|
| Architecture / edge cases / test coverage | 4 findings, all applied. Verified against source, each quoting its motivating line. |
| Scope | Complexity check tripped at 9 code files + 4 docs (threshold 8). Resolved by the A1/A2 split (§5.1). **Both counts superseded by revision 2**: the whole change is 12 code files, A1 is 5 (§5.1, §6.2, §7). |
| UX / design | §6 rewritten against `docs/design-system/README.md:369-372`; added the disable-on-submit and accepted-is-not-done states. |

Findings that changed the spec:

1. **[P1, 9/10] The headline outcome needs no backend.** `POST .../resume` already exists and is bound on every runtime (server.py:201-205). Split into A1/A2; §11 decision 10.
2. **[P1, 9/10] Every post-spawn failure reads as success.** `Popen(..., DEVNULL, DEVNULL)` + unconditional `accepted: true` (run_actions.py:90-99). New §3.9; now in scope, tests, and UX.
3. **[P1, 9/10] The 24h regression guard had no test.** §8's frontend proof was a GIF. The repo already runs real JS under Node (test_webapp_markdown.py); §8 now requires that test.
4. **[P2, 8/10] A torn claim leaves `resumable: true` forever.** `list_runs` globs `*.json`, matching `<run_id>.resuming.json` (state.py; WORKFLOW_ENGINE.md:408-412). Folded into §3.9, mitigated by the same failure-surfacing fix.

Recorded non-findings are in §3.10 so they are not re-litigated. One of them has since been **reclassified as a finding** - see revision 2 below.

**Reviewer's note on process.** The gate prescribes walking each finding through an interactive prompt. This spec was produced in an autonomous session with no human present, so findings were verified against source and applied directly, and every judgement that is genuinely the approver's was routed to §11 rather than decided here. No finding was silently dropped.

### 12.1 Revision 2 - 2026-08-12, verification pass

A second pass re-derived this spec's claims against `main` @ `8441de2` and against this deployment's live run state. **All model-based review in both passes was same-model (Claude); no cross-model second opinion was obtained** - `codex` is unauthenticated in these containers and no cross-model result is invented here.

The design **changed** as a result; this is not an appendix. Where a finding refuted a claim, the claim was rewritten in place and the retraction recorded next to it, so the wrong version is not re-derived from an old draft.

| # | Finding | Where it landed |
|---|---|---|
| R1 | **§3.3's causal story was refuted.** The count reproduces exactly (5-of-8 on snapshot A, 6-of-9 on B), but `reactor._dispatch` caused none of it: the reactor unconditionally seeds `input_fields` (reactor.py:241-246), and all waiting runs carry only the base `[repo, run_key, task]` input scope with `event_type` absent, so **none was reactor-dispatched**. The old story's correlation also failed on the spec's own table (`adhoc-*` runs sit on both sides). | §3.3 rewritten with the real cause (`--requested-by` is an optional launch flag) and the retraction recorded. §7's "Out" item re-aimed - it had pointed at a path nothing took. |
| R2 | **A file missing from scope would fail CI.** `tests/test_admin_command_parity.py:42-57` asserts `bobi/supervisor/admin.py`'s and `event-server/worker/src/fleet.ts`'s `ADMIN_COMMANDS` are equal **both ways**. `reply_run` on one side only is red CI or a silent 400. | `fleet.ts` added to §7's A2 scope; §9 step 9 lands both halves in one commit; §8 lists the existing test; §5.1 recounts A2 at seven code files. |
| R3 | **The RUN_RESUME.md claim was overstated.** The doc records no rationale (:3-5 states the removal and stops); the "honest confirm language" reading was inferred from :36-39, a passage about payload shape. The plain text - the endpoint stays "available to the CLI/framework" - cuts the other way. | §3.6 restated as a labelled inference with the counter-reading; §10 and §11 decision 8 reworded to match. |
| R4 | **Four citations were wrong.** run_actions.py:63 raises `RunNotWaiting` (the "no longer waiting" literal is :136); server.py:202 delegates to `rt.resume_run` at :205; `cli.py` resume spans 2395-**2444**; `pollRuns` is agent.js:**884**. | Corrected in §6, §3.1, §3.10. |
| R5 | **§3.1's inventory grep was presented as exhaustive but never searched `resume_run`.** The widened grep adds five real sites: `event_bus.py:475-476`, `runtime.py:878-879`, `admin.py:61/74/483`, `fleet.ts:365`. | §3.1's grep widened, printed, and every hit reclassified. |
| S1 | **The worked example silently discarded the note.** `orchestrator.py:410` is `ctx.set_scope("event", event.get("data", {}))` - it unwraps a `data` envelope, and the `.get` default means an unwrapped payload yields `{}` with **no error**. The spec's own `--event-data '{"text": ...}'` example would have shipped a feature that resumes and drops the note - the same failure class §3.9 raises. | New §4.1 makes the envelope explicit and binding; §7, §8, §9 and §10 updated. §8 now requires asserting the resulting **scope value**, because a test written from the old example passes on the broken form. |
| S2 | **"A1 delivers the whole stated goal" was an overclaim.** The issue asks for a reply box; A1 has no text input. | §1 and §5.1 state the substitution plainly instead of performing it; new §11 decision 3 asks Gate 1 to accept it knowingly. |
| S3 | "N polls" unspecified; the CSS undersold; the 5-of-8 figure presented as a constant. | N fixed at **3 polls / ~12s** with its reasoning (§3.9, §6, §8). New §6.1: no gate component exists in `app.css`, and violet currently means *live* on this page (app.css:315), so the mandated violet gate collides with an existing meaning. §3.2 now carries two dated snapshots. |
| NEW | **`reply_run` does not fit the run-action seam.** Found while checking R2. All three delegates pass `(root, run_id)` and nothing else (`runtime.py:887-892`, `admin.py:481-483`, `event_bus.py:484-489`), so a `reply_run(root, run_id, text)` is unreachable without widening the dispatch shared with the existing trio. This **reverses** revision 1's non-finding that the three-runtime binding was "correct as-is". | §3.10 reclassified; §7 and §9 step 8 carry the delegate work; A2's cost restated. |
| G1 | **The spec never asked whether the gate should exist.** It used the engine freeze as a constraint but never as a question, while the queue it serves has never been cleared once. | New §3.11 records the evidence neutrally; **new §11 decision 1**, deliberately unanswered here. |

A **same-model consistency review** of the revised draft then found four more defects, all verified against source and folded in:

| # | Finding | Where it landed |
|---|---|---|
| V1 | **`detail.suspended_at_step` is the index of the NEXT step** (`orchestrator.py:871` `step_idx + 1`; `WORKFLOW_ENGINE.md:389-390`), and it is a bare integer - `WorkflowRun` has no step-name field, and `runs.py` folds `phase` into `detail` only on the *session* branch. So §6's "names the workflow, the step and the awaited event ... no new fetch" was wrong twice: a naive confirm would name **`implement`**, the step about to run, on every one of the 9 live gates. On a control whose safety argument is that the confirm says what is being waved through, that is the worst available off-by-one. | New **§6.2**. A1 gains one additive read-model line in `runs.py`, so **A1 is five files and no longer "no backend"** (§5.1, §7). §8's confirm-string test becomes load-bearing. |
| V2 | **The torn-claim orphan was misnamed.** §3.9 said `.resuming.json`; `state.py:90-97` says `<run_id>.claiming.json` holds the pre-claim `waiting`, and `.resuming.json` is only ever written with `status="resuming"`. `close()` has the same shape via `.closing.json`. `docs/WORKFLOW_ENGINE.md:408-412` is itself stale against the code. | §3.9 corrected (conclusion unchanged, and broader - `close_run` shares it); the stale doc lines added to §7. |
| V3 | **A decision was dropped in renumbering.** Revision 2 inserted two decisions at the top of §11 and lost the Linear/GitHub scope question, leaving §2.1's "Reserved as decision 7" dangling at an unrelated item. Three other decision cross-references were off by one or more. | Scope question restored as **decision 11**; §3.6, §3.7 and §5.1 references corrected. |
| V4 | Two "printed so it can be re-run" greps did not match their own output (`runtime.py:377,386` and `app.css:378` omitted); minor line drift in five citations. | Greps completed and re-run; drift corrected. Conclusions unaffected in every case. |

**Claims re-verified and left intact**, having survived the wider inventory including string-dispatched paths: §3.1's substance (no production caller for `try_resume_for_event`), §3.4, §3.5, §3.6's engine-has-no-reject argument, §3.7, and §3.9. §5.4's corollary - `openSlab()` returns at agent.js:706 inside the `row.session_id` branch, so `renderRowDetails` is never reached for a real gate, and all 9 waiting runs carry a `session_name` - was re-confirmed against snapshot B.
