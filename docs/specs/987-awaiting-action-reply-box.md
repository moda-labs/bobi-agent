# 987 - A reply box on the run slab

Status: **awaiting Gate 1** (design approval). No code written.
Issue: [#987](https://github.com/moda-labs/bobi-agent/issues/987) · Linear MOD-372 (part of MOD-368)
Author: engineer agent · **Revision 3, 2026-08-18** (rewritten on Luke's direction, see §10)
Base: `main` @ `2c10d5a`

> **Revision 3 replaced the design.** Luke ruled out the previous one:
> ["Look into a simpler viable option that uses existing machinery"](https://github.com/moda-labs/bobi-agent/issues/987#issuecomment-5331915953).
> Revisions 1-2 specced a *resume-with-payload* control: 12 code files across Python, TypeScript and the CLI, a new endpoint, a new run-action verb, a widened dispatch shape and both `ADMIN_COMMANDS` halves.
> That design is withdrawn in full, not amended. This revision is the simpler option, and it is smaller because the machinery already exists.

---

## 1. Summary

Luke's model is right, and it is already built on the server.

The chat path the terminal uses is `bobi agent <name> message --wait` and its alias `ask`, and both funnel into one function, `inbox.deliver` (§3.1).
The webapp already speaks that exact path: `POST /api/agents/{name}/chat` plus a poll on `GET /api/agents/{name}/chat/{message_id}`, bound on **both** runtimes, implemented by the supervisor, and already allowlisted in the Cloudflare Worker (§3.2).
The modal already renders the conversation (§3.3).

So the feature is a **frontend composer** in the slab, posting to endpoints that ship today.

**Backend cost: one additive read-model field** (§5.3), and that field exists to stop the box from lying rather than to move a message.
No new endpoint, no new CLI flag, no new run-action verb, no `ADMIN_COMMANDS` change, no engine change, no Slack.

**The one thing that does not work, and cannot be made to work by this feature.**
The issue's literal target is the *awaiting-action* row. A suspended workflow gate has **no live agent to talk to**: the orchestrator disconnects its client and returns at the await step (orchestrator.py:891-896), so the process exits. All 10 gates waiting in this deployment right now have dead pids, and `inbox.deliver` refuses a dead pid outright (§4).
**The terminal cannot chat with them either.** That is not a limitation this design accepts in place of Luke's; it is Luke's premise holding exactly. "Nearly identical to the terminal" means a suspended gate is not a conversation partner on either surface.

So the reply box serves **live sessions**, which is what Linear MOD-372 asks for in its own title ("Give users the ability to chat with a **running** agent") and is narrower than GitHub #987's title.
That gap is the single thing Gate 1 must accept (§9, decision 1).

---

## 2. What Luke's ruling deletes

Recorded so the withdrawn design is not re-derived from an old draft.

| Withdrawn | Why it is gone |
|---|---|
| The whole reply-and-continue / resume-with-payload design | Ruled out as extra machinery. It was 12 code files, a new endpoint, a new verb, a widened three-runtime dispatch and both `ADMIN_COMMANDS` halves. |
| The `{"data": {...}}` event-payload envelope and its silent-drop trap | No payload travels. Nothing to envelope. |
| The Slack-thread option, and the "6 of 9 gates have no thread" evidence for it | Luke: "would not need extra machinery or connection to slack". |
| Gating on `awaiting_action` vs `detail.resumable`, and the 24-hour-derived-status analysis | The composer is gated on session liveness, which is neither. |
| The violet gate block, the rotated-square glyph, the step-name read-model widening | This is a conversation composer, not a gate block. The design system's gate rule governs approval surfaces, and this is not one. |
| Resume's fire-and-forget failure mode, the torn-claim orphan, the reminder string's false promise | Real, pre-existing, and **not this ticket's**. They belong to the resume path, which this design does not touch. Named in §6 as out, not smuggled in. |

Two things survive, because they are about *this* surface:

- The plan that produced this page removed the chat column on purpose (§3.5).
- The population this serves is small and the gate rows are not in it (§4.3).

---

## 3. The machinery that already exists

Every claim below was derived in a worktree at `main` @ `2c10d5a`, and every probe was executed against this deployment's live state with `bobi` imported from that worktree.

### 3.1 The terminal and the web UI already share one transport

"Chat with the agent via terminal" is `bobi agent <name> message` (cli.py:972-1002) and its hidden alias `ask` (cli.py:1038-1041). Both are one-shot request/reply, not a REPL:

```
cli.py:3688-3694
for _cmd_name in [
    "start", "stop", "restart", "status", "ui", "message", "ask", "compact",
    "events", "costs", "doctor", "login-bootstrap", "recall-memory",
    "supervise",
]:
    if _cmd_name in main.commands:
        agent.add_command(main.commands[_cmd_name])
```

`message` calls `service.send_message` (cli.py:989-991), which resolves an address and delivers:

```
service.py:780-783  (send_message, defined at 764)
    from bobi.inbox import deliver

    ok, response = deliver(address, text, sender=sender, wait=wait,
                           timeout=timeout, root=project_path)
```

The webapp's chat lands on the same line. `POST .../chat` → `rt.chat_submit` → `service.ask` (runtime.py:697-699) → `send_message(..., wait=True)` (service.py:806-807) → the same `deliver`.

Mechanically, the production delivery seam is one function with two callers:

```
$ grep -rn "from bobi.inbox import deliver" --include=*.py bobi/
bobi/service.py:780
bobi/cli.py:1020
```

Both hits classified:

| Hit | What it is |
|---|---|
| service.py:780 | The seam. Every terminal `message`/`ask` **and** every webapp `/chat` reaches `deliver` through here. |
| cli.py:1020 | `bobi agent <name> compact`, which delivers a `COMPACT_SENTINEL` rather than a chat turn (cli.py:1029). Not a chat path. |

So a modal reply box is not "like" the terminal. Routed through `/chat`, it **is** the terminal's transport, one call deeper.

### 3.2 The webapp chat backend is complete, on both runtimes and in the Worker

The endpoint pair ships:

```
server.py:356-365
    @app.post("/api/agents/{name}/chat")
    def chat(name: str, payload: dict) -> JSONResponse:
        subagent = (payload.get("subagent") or "").strip()
        text = (payload.get("text") or "").strip()
        ...
        return JSONResponse({"message_id": rt.chat_submit(name, subagent, text)})

server.py:367-372
    @app.get("/api/agents/{name}/chat/{message_id}")
    def chat_status(name: str, message_id: str) -> JSONResponse:
        job = rt.chat_job(name, message_id)
```

Its own comment already describes the client this ticket would write:

```
server.py:350-355
    # Submit-then-poll chat: the POST returns a message id immediately and
    # the deliver runs in the background — no request is held open for the
    # agent's (up to minutes-long) reply, so the endpoint shape survives
    # proxies and load balancers (the #525 SaaS discipline). The reply
    # reaches the transcript via the messages poll; the job carries only
    # status and errors.
```

The binding inventory, printed so it can be re-run:

```
$ grep -rn "chat_submit\|chat_job" --include=*.py --include=*.ts --include=*.js bobi/ event-server/
bobi/webapp/server.py:365:        return JSONResponse({"message_id": rt.chat_submit(name, subagent, text)})
bobi/webapp/server.py:369:        job = rt.chat_job(name, message_id)
bobi/webapp/event_bus.py:491:    def chat_submit(self, name: str, session: str, text: str) -> str:
bobi/webapp/event_bus.py:499:    def chat_job(self, name: str, message_id: str) -> dict | None:
bobi/webapp/runtime.py:186:    def chat_submit(self, name: str, session: str, text: str) -> str:
bobi/webapp/runtime.py:190:    def chat_job(self, name: str, message_id: str) -> dict | None:
bobi/webapp/runtime.py:559:        self._chat_jobs: dict[str, dict] = {}
bobi/webapp/runtime.py:681:        if len(self._chat_jobs) <= 500:
bobi/webapp/runtime.py:683:        for mid in [m for m, j in self._chat_jobs.items()
bobi/webapp/runtime.py:685:            self._chat_jobs.pop(mid, None)
bobi/webapp/runtime.py:687:    def chat_submit(self, name: str, session: str, text: str) -> str:
bobi/webapp/runtime.py:692:            self._chat_jobs[message_id] = {"team": name, "status": "pending"}
bobi/webapp/runtime.py:704:                self._chat_jobs[message_id] = outcome
bobi/webapp/runtime.py:710:    def chat_job(self, name: str, message_id: str) -> dict | None:
bobi/webapp/runtime.py:712:            job = self._chat_jobs.get(message_id)
```

Classifying every hit:

| Hit | What it is |
|---|---|
| server.py:365, 369 | The two routes. |
| runtime.py:186, 190 | The abstract `TeamRuntime` contract. Both methods are part of it already. |
| runtime.py:687-708, 710-716 | **`LocalRuntime`** (`bobi app`): submits on a background thread, `service.ask` with a 300s budget (runtime.py:28, 699). |
| runtime.py:559, 681-685, 692, 704, 712 | The in-process job table and its 500-entry prune. Internals of the above. |
| event_bus.py:491-497, 499-516 | **`EventBusRuntime`** (the hosted fleet path): issues the `chat` command over the bus and folds the result. |

Both runtimes bound, and the hosted half is allowlisted on both sides of the wire:

```
$ grep -rn '"chat"' event-server/worker/src/fleet.ts bobi/supervisor/admin.py
event-server/worker/src/fleet.ts:355:	"chat",
bobi/supervisor/admin.py:56:                            "chat", "transcript", "roster", "spend",
bobi/supervisor/admin.py:250:        if command == "chat":
```

`fleet.ts:350-368`'s `ADMIN_COMMANDS` and `admin.py:55-61` must match exactly or `tests/test_admin_command_parity.py` fails.
`chat` is in both **today**, so this ticket does not touch either list. That parity edit was the withdrawn design's most expensive hidden cost, and it is now simply absent.

The supervisor even handles chat asynchronously already, for the same reason the route does:

```
admin.py:247-251
        # Chat is the one async command: hand off to a detached worker that owns
        # the result publish, so the (potentially minutes-long) turn never blocks
        # this ordered dispatch worker or a queued restart.
        if command == "chat":
            self._dispatch_chat_async(command_id, args)
```

### 3.3 The modal already renders the conversation

```
agent.js:681-689  (openSlab, defined at 674)
    // Rows with a session get a transcript; rows without get details.
    // That is the rule, and it is decided by data rather than by kind.
    if (row.session_id) {
      els.slabKind.textContent = "transcript";
      const { ok, data } = await api(
        `${base}/subagents/${encodeURIComponent(row.session_id)}/transcript`);
      if (!ok || !data) return slabError("Could not read that transcript.");
      renderTranscript(row, data);
      return;
    }
```

`renderTranscript` (agent.js:712-745) prints one line per entry with a role. That is the "transcript/conversation" Luke is pulling up.

It reads from disk by session id, so it works for a finished session too. Probed against this deployment:

```
wf-issue-lifecycle-eng-team-987              status=waiting  transcript_entries=200
bobi-eng-team-director                       status=idle     transcript_entries=200
```

`200` here is the cap, not a count: `read_transcript_detail` returns the last `CHAT_HISTORY_LIMIT` entries (chat_history.py:28, 256-257, 299). Both transcripts are therefore full rather than empty, the gate's included.

Worth stating precisely: on a gate row the modal **does** show a real conversation. It is only the *reply* that has no recipient (§4).

### 3.4 What is genuinely missing

One thing, and it is entirely frontend:

```
$ grep -n "textarea\|<input" bobi/webapp/static/views/agent.js
124:            <input class="runs-search" data-el="runsSearch" type="search"
```

The page's only text-entry control is the runs search box. There is no composer, and no code that calls `/chat`.

A second, smaller gap: **the slab is one-shot**. `openSlab` fetches the transcript once (agent.js:685-688) and nothing re-fetches it. The 4s timers refresh the table, not the slab:

```
agent.js:865-867
  timers = [
    setInterval(pollHealth, 4000),
    setInterval(pollRuns, 4000),
```

So after a reply resolves, the slab must re-fetch its own transcript to show the answer. That is a few lines in the same function, and it is the only new *behaviour* beyond the box itself.

The CSS is partly there already: `.chat`, `.chat-head`, `.chat-name` (app.css:601-610) and `.chat-ended` (app.css:502) survive from the removed chat column.

### 3.5 The plan that built this page removed the chat column on purpose

```
agent.js:10-12
   This replaced the five-panel page (needs-attention, health, spend,
   roster, session log) plus the chat column. Chat lives in Slack and the
   CLI; this page observes and recovers.
```

```
plans/2026-07-31-single-agent-view.md:129-130
- *Chat column removed* — Slack/CLI are the chat surfaces; the page is for
  observing and recovering.
```

That plan is **Locked (design approved 2026-07-31)** (plans/2026-07-31-single-agent-view.md:3).
This ticket puts a typing surface back on that page. Luke's comment is direction to do exactly that, and it is the newer statement, but the plan is the older *approved* one and CLAUDE.md is explicit that "post-approval changes are dated amendments, never silent rewrites".

So the implementing PR carries a **dated amendment** to `plans/2026-07-31-single-agent-view.md` recording that the page regained a reply surface and why.
That is the honest mechanism, and it is cheap. Confirming it is decision 3 in §9.

Note what the plan's reason permits and what it does not: it removed a chat **column**, a persistent panel beside the table. This is a composer inside a modal the operator deliberately opened on one session. Narrower, and it does not restore the panel.

---

## 4. Why the awaiting-action row is the one row this cannot serve

This is the load-bearing limitation, and it is not a choice.

### 4.1 A suspended gate's process is gone

```
orchestrator.py:861-863  (the await step)
            if step.await_event:
                log.info(f"Await step {step.name}: suspending, waiting for '{step.await_event}'")
                registry.update(session_name, status="waiting", phase=step.name)
```

```
orchestrator.py:891-896
                suspended = True
                try:
                    await client.disconnect()
                except Exception:
                    pass
                return True
```

The registry entry stays `waiting`, and the process exits.
The entry is never corrected, because the reaper only inspects live-looking statuses:

```
sdk.py:580-581  (inside _reap_if_dead, defined at 571)
        if entry.status not in ACTIVE_STATUSES:
            return entry
```

`ACTIVE_STATUSES = ("starting", "running", "idle")` (sdk.py:44). `waiting` is in neither that tuple nor `DEAD_STATUSES`, so a gate keeps a `waiting` status and a stale pid indefinitely.

Measured on this deployment, all 10 waiting gates:

```
run_id     await        session_name                                sess.status pid      proc
11d31ce5   'approval'   wf-issue-lifecycle-eng-team-987             waiting     19290    DEAD
2a6c8c9e   'approval'   wf-issue-lifecycle-eng-team-958             waiting     6628     DEAD
409b4300   'approval'   wf-issue-lifecycle-eng-team-933             waiting     8513     DEAD
46aecc34   'approval'   wf-issue-lifecycle-eng-team-adhoc-60c70cd8  waiting     7951     DEAD
75881342   'approval'   wf-issue-lifecycle-eng-team-adhoc-016d4503  waiting     1415     DEAD
7eec97d1   'approval'   wf-issue-lifecycle-eng-team-1006            waiting     31020    DEAD
a11ece76   'approval'   wf-issue-lifecycle-eng-team-1016            waiting     28010    DEAD
b0529f37   'approval'   wf-issue-lifecycle-eng-team-adhoc-57133a39  waiting     15569    DEAD
e097927a   'approval'   wf-issue-lifecycle-eng-team-adhoc-e0dc9a95  waiting     5786     DEAD
e5f2573a   'approval'   wf-issue-lifecycle-eng-team-adhoc-55df2bbc  waiting     5083     DEAD
```

Reproduce (reads `state/workflow/runs/*.json`, then each run's `state/sessions/<session_name>/state.json`, then `os.kill(pid, 0)`):

```bash
python3 - <<'PY'
import json, glob, os
for p in glob.glob('<run>/state/workflow/runs/*.json'):
    r = json.loads(open(p).read())
    if r.get("status") != "waiting": continue
    sn = r.get("session_name") or ""
    s = json.loads(open(f'<run>/state/sessions/{sn}/state.json').read())
    try: os.kill(int(s["pid"]), 0); alive = "ALIVE"
    except ProcessLookupError: alive = "DEAD"
    print(r["run_id"][:8], repr(r.get("await_event")), sn, s["status"], s["pid"], alive)
PY
```

### 4.2 The terminal refuses it too, and that is the point

The inbox is in-process state, not a mailbox on disk:

```
inbox.py:3-8
Every session has an inbox: an in-memory queue its run loop drains. Messages
arrive as ``inbox/<session>`` events on the configured event server, are
delivered over the session's subscription/drain path (the same path lifecycle
events use), and pushed into this queue by the drain loop (see
``events/drain.py``). There is no per-session HTTP server — the inbox is
purely in-process state; the transport is the event server.
```

So `deliver` checks the target is alive before publishing:

```
inbox.py:309-322  (deliver, defined at 280)
    entry = SessionRegistry(project_path).get(to)
    if not entry:
        return False, f"session '{to}' not found"

    if entry.pid and not pid_alive(entry.pid):
        return False, f"session '{to}' process is dead"
    ...
    if entry.status in DEAD_STATUSES:
        return False, f"session '{to}' is {entry.status}"
```

Executed against a live gate, from the worktree under test:

```
bobi loaded from: .../worktrees/987-luke-simpler/bobi/__init__.py
gate session: status=waiting pid=19290
deliver(gate) -> (False, "session 'wf-issue-lifecycle-eng-team-987' process is dead")
ask(gate)   -> MessageDeliveryError: unknown agent 'wf-issue-lifecycle-eng-team-987'
```

Two independent refusals, and they are not the same refusal:

- **`deliver`** refuses on the **dead pid**. This is the fundamental one, and it is what `bobi agent <name> message --to <gate-session>` hits from a terminal.
- **`ask`** refuses earlier, on its own membership guard (`agent not in list_agents(...)`, service.py:803-804), because `list_agents` → `list_active()` → `ACTIVE_STATUSES` (service.py:734, 738-739; sdk.py:594-596). `ask`'s docstring says this guard is deliberate: "a caller (e.g. a web UI) can never fan a message at an arbitrary name" (service.py:799-801).

Relaxing `ask`'s guard would change nothing, because `deliver` refuses one layer down on a fact about the operating system.
**There is no live process to receive a message.** No amount of webapp work creates one.

### 4.3 So who does this serve

At the moment of writing, this deployment's registry holds 432 sessions: 309 completed, 44 error, 33 cancelled, 22 failed, 12 crashed, **10 waiting**, 1 idle, 1 running.

`list_active()` returns **2**: the director (`idle`) and the engineer session writing this spec (`running`).

That is the chattable population, and it is exactly the population the terminal can reach. It is small because it is a *liveness* set, not a backlog: it is whatever is running right now.

Stated plainly so Gate 1 is not surprised: **0 of the 10 awaiting-action gates are in it**, and the runs table's `awaiting_action` rows are precisely the rows where the box will not appear.

---

## 5. The design

### 5.1 Where it goes

A composer appended in the slab's **transcript branch**, under the rendered conversation (agent.js:683-689).

Rows without a `session_id` take the details branch (agent.js:692-704) and get no composer. There is no session, so there is nothing to talk to.

No change to `rowActions()` (agent.js:609-640). The row keeps Transcript / Details / Close as it is today.

### 5.2 What it does

1. Textarea plus a submit control. Submit disables both, following the `closeRun()` idiom already in the file (agent.js:643-662).
2. `POST ${base}/chat` with `{subagent: row.session_id, text}` → `{message_id}`.
3. Poll `GET ${base}/chat/${message_id}` until `status !== "pending"`.
4. On `done`: re-fetch `${base}/subagents/${session_id}/transcript` and re-render, so the operator sees the answer without reopening the slab (§3.4).
5. On `error`: render `job.error` inline via the existing `slabError` idiom (agent.js:707-710). Never a toast.

The turn budget is the server's, not the client's: `DEFAULT_CHAT_TIMEOUT = 300` (runtime.py:28), applied at runtime.py:699. The poll must outlive it or report honestly when it does not.

### 5.3 The one backend change, and why it is not optional

The composer must render **only** when the session can receive a message. Otherwise it is a box that accepts typing and then reports `unknown agent`, which is the false-promise failure this page already has too much of.

The row cannot answer that question reliably today. The read model says so itself:

```
runs.py:42-44
# What the RUNNING tab holds. `idle` is a live-but-waiting manager or a
# freshly suspended workflow — present, not working.
LIVE_STATUSES = (RUNNING,)
```

`idle` is ambiguous by construction: `_session_status` maps a live idle session to `IDLE` (runs.py:122-123) and `_workflow_status` maps a freshly suspended gate to the same `IDLE` (runs.py:185-190).

**Proposal: one additive field, `detail.live`, stamped in `build_runs`.**
`build_runs` already reads the registry at runs.py:378 (`SessionRegistry(root).list_all(reap_dead=True)`) before building any rows, so the live-name set is in hand. Stamping every row that carries a `session_id` covers session, workflow and monitor rows uniformly, in one function.

It is the same shape and spirit as the `resumable` field that already sits in the workflow branch:

```
runs.py:226
                    "resumable": run.status == "waiting"},
```

Additive, no existing field changes shape, no new endpoint, no engine change.

Note the predicate is correct *because* the reaper is lenient (§4.1): a `waiting` entry keeps a stale pid, but `waiting ∉ ACTIVE_STATUSES`, so it is excluded anyway.

**The alternative, rejected.** The frontend could derive liveness as `status === "running" || (kind === "session" && status === "idle")`. That is true today, but only as a consequence of four separate invariants: the `_session_status` mapping (runs.py:115-132), `LIVE_STATUSES = (RUNNING,)` (runs.py:44), the promotion rule in `_claim_sessions` (runs.py:329-331) and the claim/drop rule above it (runs.py:313-323). Any one of them moving breaks the composer silently, and in the direction of showing a box that cannot work. Re-deriving in the client a fact the server already holds is the more expensive option, not the cheaper one.

### 5.4 What a non-live session shows instead

One line where the composer would be, naming the reason: the session has finished, or is a suspended workflow gate with no live agent.
For a gate row, that line is the honest version of what §4 establishes, and the row's existing **Close** button remains the only write.

This is deliberately not a link to the resume path. Offering "approve this gate" from a box labelled reply is the conflation the withdrawn design was criticised for.

---

## 6. Scope

**In**

- `bobi/webapp/static/views/agent.js`: the composer in the transcript branch, the submit-then-poll cycle, the post-reply transcript re-fetch, the disabled state and its reason line.
- `bobi/webapp/static/app.css`: composer styling, extending the surviving `.chat*` block (app.css:601-610).
- `bobi/webapp/runs.py`: the single additive `detail.live` field (§5.3).
- `tests/`: §7.
- Docs: `docs/RUN_DRILLDOWNS.md` (the slab gains a write surface), `docs/RUNS_VIEW.md` (the new `detail` field).
- `plans/2026-07-31-single-agent-view.md`: dated amendment recording the reply surface (§3.5), subject to decision 3.

**Out**

- **Anything on the resume path.** The fire-and-forget spawn whose failures read as success, the torn-claim orphan, and the reminder step's "Reply in this thread to continue" promise are all real and all pre-existing. They belong to resume, which this design does not touch. Recorded here so they are not lost, and deliberately not bundled.
- Making a suspended gate answerable. §4: no live process exists. That is a workflow-engine question, and the engine is frozen.
- Relaxing `ask`'s membership guard (service.py:803-804). It would change nothing (§4.2) and it exists on purpose.
- `try_resume_for_event`, any approve/reject vocabulary, any Slack path.
- A persistent chat panel. The plan removed the column (§3.5); this is a composer inside a modal.
- `VERSION`, `pyproject.toml` version, `CHANGELOG.md`. Untouched.

---

## 7. Verification plan

The risk is a box that accepts typing and delivers nothing, so the tests must prove **the message reached the agent**, not that a POST returned 200.

**Unit (Python)**

- `build_runs` stamps `detail.live` true for a session in `ACTIVE_STATUSES` and false for a `waiting` gate row whose pid is dead. This is the guard that keeps the composer honest, and it is the one test that must exist.
- `detail.live` is false for a `waiting` workflow row **even though its status renders as `idle`**. This pins the §5.3 ambiguity directly.

**Frontend, executed as real JavaScript under Node**

The repo already has this harness and it is not Playwright:

```
tests/test_webapp_markdown.py:1, 6-7
"""The webapp's agent-reply markdown renderer, executed as real JavaScript.
...
Asserting on the JS source text would prove nothing about what it renders, so
these run the actual module under Node and parse what comes back.
```

Following that pattern:

- The composer renders when `detail.live` is true, and does not when it is false.
- The disabled case renders its reason line rather than an enabled control.
- Submit posts `{subagent, text}` to `/chat` and disables the control.
- A `done` job triggers exactly one transcript re-fetch and re-render (§3.4).
- An `error` job renders `job.error` inline, and the control re-enables.

**Integration (isolated `BOBI_HOME`, real sessions)**

- Against a **live** session: POST `/chat`, poll the job to `done`, and assert the text arrived as a turn in that session's transcript. This is the test that proves the feature.
- Against a **suspended gate's** session: assert the delivery is refused with `process is dead` rather than silently accepted. This pins §4 as a regression guard, so a future change that resurrects the box for gate rows fails here.

**Brain**: a real-Claude leg is warranted. Per CLAUDE.md's "one mechanism, two brains" rule, this is event delivery through a live session and a turn taken by the brain, which is the case the rule names rather than the brain-agnostic control-plane case. Parametrize `[stub]+[claude]`, gate the claude leg on the CLI.

**Manual capture**: drive the real page against a live session and attach a GIF of type → send → the reply appearing in the slab, per the house proof-of-work rule.

---

## 8. Implementation plan

Tests first, each step independently reviewable.

1. `runs.py` `detail.live`, with its two unit tests red first.
2. Node-executed JS tests from §7, red.
3. The composer in `agent.js` plus `app.css`: render, gate on `detail.live`, submit-then-poll, re-fetch, inline error, disabled reason line.
4. Integration tests, both directions (live session delivers, gate refuses).
5. Docs (§6) and the dated plan amendment (decision 3).
6. Review gate, full suite, frontend capture, PR.

---

## 9. Decisions reserved for Gate 1

1. **Accept that this serves live sessions, not awaiting-action gates.**
   The issue's title asks for a box on the awaiting-action slab. §4 shows there is no live agent behind those rows and the terminal cannot reach them either, so the box appears on live sessions instead.
   That lands on Linear MOD-372's own title ("chat with a **running** agent") and is narrower than GitHub #987's. It should be accepted knowingly, and #987's title updated to match.
2. **Accept the one additive read-model field** (`detail.live`, §5.3), or direct me to derive liveness client-side and take the fragility.
3. **Confirm the dated plan amendment** (§3.5). The Locked plan removed the chat column with a stated reason; this puts a composer back. My reading is that an amendment is the right mechanism and the composer is narrow enough to be uncontroversial. If you disagree, say whether the objection is to the surface or to the amendment.
4. **Confirm the out-of-scope list** (§6), in particular that the resume path's pre-existing defects stay unbundled.

No code will be written until these are answered.

---

## 10. Revision record

### Revision 3 - 2026-08-18, rework on Luke's direction

Luke's [comment](https://github.com/moda-labs/bobi-agent/issues/987#issuecomment-5331915953) ruled the previous design out and asked for "a simpler viable option that uses existing machinery".

**It is viable, and the reason it is simpler is that the server half already shipped.** The finding that changed the shape of this document is §3.2: the chat endpoint pair, both runtime bindings, the supervisor handler and the Worker allowlist all exist today, so the previous design's most expensive items (a new verb, a widened three-runtime dispatch, both `ADMIN_COMMANDS` halves) are not merely cheaper, they are absent.

| | Revision 2 | Revision 3 |
|---|---|---|
| Code files | 12 (Python + TypeScript + CLI) | 3 (`agent.js`, `app.css`, `runs.py`) |
| New API surface | 1 endpoint, 1 run-action verb | none |
| `ADMIN_COMMANDS` edits | both halves | none (`chat` already in both) |
| Engine / CLI changes | a string edit, `--event-data` | none |
| Spec length | 1026 lines | this |

**The one thing that got harder, not easier.** The withdrawn design could pass an awaiting-action gate, because a resume spawns a fresh process. This one cannot talk to a gate at all, because there is no process (§4). That is a real reduction in what the feature does against #987's literal title, it is forced by the operating system rather than chosen, and it is decision 1 rather than a footnote.

**Verification discipline.** Every citation in this revision was re-derived at `main` @ `2c10d5a` in the worktree that produced it, and the live probes in §4 were executed with `bobi` imported from that same worktree after an earlier probe was found reading a stale parked checkout. Inventories in §3.1 and §3.2 print their grep and classify every hit.
`#989`/MOD-371 landed while this was parked (86cd7e1), so revision 2's "post-#989" baseline is now simply current: `rowActions()` offers Transcript / Details / Close (agent.js:609-640).

**Not obtained:** a cross-model second opinion. `codex` is unauthenticated in these containers; no cross-model result is invented here.

### Revisions 1-2 - 2026-08-12

Specced reply-and-continue: the typed note carried as the resumed run's `event` payload. Withdrawn in full by revision 3, see §2 for what that deletes and why.
Their verification work is not lost: the facts about the resume path (fire-and-forget spawn, the torn-claim orphan, the reminder string's false promise) remain true and are recorded as out of scope in §6 rather than carried as design.
