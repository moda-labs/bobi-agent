# 987 - A reply box on the run slab

Status: **approved and built** (Gate 1 granted by Luke, 2026-08-18: ["Approved, let's go ahead and build this feature and test it"](https://github.com/moda-labs/bobi-agent/issues/987#issuecomment-5334520258)). Reworked on Zach's review, 2026-08-19; see §12.
Issue: [#987](https://github.com/moda-labs/bobi-agent/issues/987) · Linear MOD-372 (part of MOD-368)
Author: engineer agent · **Revision 5, 2026-08-19** (the gate is answered, see §12)
Base: `agent/987` @ `c803436`, cut from `main` @ `4725c67`

> **Revision 5 replaces §6.** Zach reviewed what revision 4 built and rejected
> its continuation branch: relaying the operator's words to the team manager so
> it could start a fresh session is a shim, and a parked session should be
> resumed. He approved the refactor.
> **§6 is superseded in full and §12 is the current design.** §6 is kept because
> its findings are still true and still load the new one - above all §6.2, which
> established that a suspended run records the step AFTER its gate. §12 does not
> work around that fact; it uses it.

> **Revision 4 adds one requirement.** Luke accepted revision 3's scope shift on a condition, in Slack on 2026-08-18:
> *"I think that's okay, as long as the user's input on that modal can create a fresh session that picks up where the last one left off"*.
> So the box no longer just refuses on a non-live row. It gets a second branch, specified in §6.
> The condition is achievable on machinery that ships, and §6.5 says exactly what it costs and what it cannot promise.
> Revision 3's design is unchanged underneath; §1-§5 stand as written and were re-verified at this sha.

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

**Backend cost: one additive read-model field** (§5.3) plus **one line** that closes a runtime asymmetry (§6.4).
No new endpoint, no new CLI flag, no new run-action verb, no `ADMIN_COMMANDS` change, no engine change, no Slack.

**A suspended gate still cannot be chatted with, and no webapp work changes that.**
The orchestrator disconnects its client and returns at the await step (orchestrator.py:891-896), so the process exits. All 10 gates waiting in this deployment have dead pids, and `inbox.deliver` refuses a dead pid outright (§4).
**The terminal cannot chat with them either.** That is Luke's premise holding exactly: "nearly identical to the terminal" means a suspended gate is not a conversation partner on either surface.

**What revision 4 adds is the other half of that row.** Typing into the modal on a non-live target no longer just reports why the box is disabled. It sends the operator's text to the live team manager, which starts a **fresh** session carrying the finished run's durable context forward (§6). That is Luke's condition, and §6.5 states what it genuinely costs and the one thing it cannot guarantee.

So the box has two branches: **talk to a live session**, or **continue a finished one**. Neither resumes the parked workflow step, and §6.2 shows why that distinction is the whole safety story.
Gate 1 still accepts that the *chat* branch serves live sessions only, which is Linear MOD-372's own title ("Give users the ability to chat with a **running** agent") and narrower than GitHub #987's (§10, decision 1).

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
That is the honest mechanism, and it is cheap. Confirming it is decision 3 in §10.

Note what the plan's reason permits and what it does not: it removed a chat **column**, a persistent panel beside the table. This is a composer inside a modal the operator deliberately opened on one session. Narrower, and it does not restore the panel.

---

## 4. Why the awaiting-action row cannot be chatted with

This is the load-bearing limitation, and it is not a choice. It is why the box needs a second branch (§6) rather than a wider chat.

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
(import path: .../worktrees/987-luke-simpler/bobi/__init__.py)
gate session: status=waiting pid=19290
deliver(gate) -> (False, "session 'wf-issue-lifecycle-eng-team-987' process is dead")
ask(gate)   -> MessageDeliveryError: unknown agent 'wf-issue-lifecycle-eng-team-987'
```

Two independent refusals, and they are not the same refusal:

- **`deliver`** refuses on the **dead pid**. This is the fundamental one, and it is what `bobi agent <name> message --to <gate-session>` hits from a terminal.
- **`ask`** refuses earlier, on its own membership guard (`agent not in list_agents(...)`, service.py:803-804), because `list_agents` → `list_active()` → `ACTIVE_STATUSES` (service.py:734, 738-739; sdk.py:594-596). `ask`'s docstring says this guard is deliberate: "a caller (e.g. a web UI) can never fan a message at an arbitrary name" (service.py:799-801).

Relaxing `ask`'s guard would change nothing, because `deliver` refuses one layer down on a fact about the operating system.
**There is no live process to receive a message.** No amount of webapp work creates one.

### 4.3 So who does the chat branch serve

At the moment of writing, this deployment's registry holds 432 sessions: 309 completed, 44 error, 33 cancelled, 22 failed, 12 crashed, **10 waiting**, 1 idle, 1 running.

`list_active()` returns **2**: the director (`idle`) and the engineer session writing this spec (`running`).

That is the chattable population, and it is exactly the population the terminal can reach. It is small because it is a *liveness* set, not a backlog: it is whatever is running right now.

Stated plainly so Gate 1 is not surprised: **0 of the 10 awaiting-action gates are in it**. Those rows still get a composer, but it takes the continuation branch (§6), not a delivery.

---

## 5. The design

### 5.1 Where it goes

A composer appended in the slab's **transcript branch**, under the rendered conversation (agent.js:683-689).

Rows without a `session_id` take the details branch (agent.js:692-704) and get no composer. There is no session, so there is nothing to talk to.

No change to `rowActions()` (agent.js:609-640). The row keeps Transcript / Details / Close as it is today.

### 5.2 What it does, on a live session

This is the **reply** branch. The **continue** branch, for a non-live target, is §6.4; both share steps 1, 3 and 5.

1. Textarea plus a submit control. Submit disables both, following the `closeRun()` idiom already in the file (agent.js:643-662).
2. `POST ${base}/chat` with `{subagent: row.session_id, text}` → `{message_id}`.
3. Poll `GET ${base}/chat/${message_id}` until `status !== "pending"`.
4. On `done`: re-fetch `${base}/subagents/${session_id}/transcript` and re-render, so the operator sees the answer without reopening the slab (§3.4).
5. On `error`: render `job.error` inline via the existing `slabError` idiom (agent.js:707-710). Never a toast.

The turn budget is the server's, not the client's: `DEFAULT_CHAT_TIMEOUT = 300` (runtime.py:28), applied at runtime.py:699. The poll must outlive it or report honestly when it does not.

### 5.3 The one read-model change, and why it is not optional

The composer must know **which branch it is on**: deliver to this session, or continue it in a new one (§6). Get that wrong in the permissive direction and it is a box that accepts typing and then reports `unknown agent`, which is the false-promise failure this page already has too much of.

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

### 5.4 What a non-live session does instead

Revision 3 stopped here, with a disabled box and a line naming the reason. **Revision 4 replaces that with a second branch**, specified in §6: the composer stays enabled, its label changes, and the send starts a fresh session instead of delivering to a dead one.

The reason line survives inside that branch, because the operator still has to be told the target is not listening. What does not survive is the dead end.

Unchanged either way: this is **never** a link to the resume path. Offering "approve this gate" from a box labelled reply is the conflation the withdrawn design was criticised for, and §6.2 shows it is also the one action that would actively destroy work.

---

## 6. Luke's condition: the continuation branch

> **Superseded by §12 (revision 5, 2026-08-19).** The continuation branch
> specified here was built, reviewed, and rejected: a parked run is resumed
> now, not relayed around. Read this section for its findings - §6.2's sharp
> edge in particular - not for its design. Everything it describes as shipping
> has been deleted from the tree.


> *"I think that's okay, as long as the user's input on that modal can create a fresh session that picks up where the last one left off"* - Luke, Slack, 2026-08-18.

Everything below was verified first-hand in a worktree at `agent/987` @ `99f5e23`, and the live probes were executed with `bobi` imported from that worktree.

### 6.1 What "picks up where it left off" can honestly mean

Bobi already has a resume contract, and it is keyed by **session name**:

```
sdk.py:657-666  (save_session_id, defined at 640)
    (sd / f"{name}.id").write_text(session_id)
    model_path = sd / f"{name}.model"
    brain_path = sd / f"{name}.brain"
```

Three sidecars per session, and the parked #987 run has all three on disk today:

```
$ cd <run>/state/sessions && cat wf-issue-lifecycle-eng-team-987.{id,brain,model}
874c2dad-41e8-4201-ac1a-1f8cc3955e62      # .id    - the brain's resume token
claude                                    # .brain - which brain minted it
claude-opus-5                             # .model - which model it ran under
```

`load_resumable_session_id` (sdk.py:690) hands that token back only when the recorded brain matches the active one and `continuation_token` (brain/__init__.py:453-484) allows the model transition. Probed against the parked run:

```
transcript messages on disk: 40   (read_transcript_messages)
transcript detail entries:  200   (read_transcript_detail, = CHAT_HISTORY_LIMIT)
resumable @ claude-opus-5  : '874c2dad-…'
resumable @ claude-sonnet-5: '874c2dad-…'   (Claude has cross_model_resume)
```

So a real, resumable transcript exists. **Two things carry, and they are not the same thing:**

| Carrier | What it holds | Survives a fresh session? |
|---|---|---|
| The brain transcript (`.id`) | The literal conversation, up to 200 rendered entries | No - it is a resume token, and resuming needs the same session name (§6.3) |
| The run's variable scopes | Every prior step's handoff, persisted on the run record | **Yes** - it is data, and any prompt can carry it |

The parked #987 run's scopes, read from `state/workflow/runs/11d31ce5.json`:

```
scopes: ['input', 'setup', '_flat', 'pickup', 'spec', 'plan_review', '_runtime']
```

Four of those are the handoffs the run actually wrote (`setup`, `pickup`, `spec`, `plan_review`), matching the four `handoff-*.yaml` files still sitting in `state/sessions/wf-issue-lifecycle-eng-team-987/`. The framework already has a function that turns them into an opening prompt for a fresh session:

```
orchestrator.py:553-565  (_continuation_prompt)
        return (
            f"Continue workflow `{workflow.name}` for issue #{run_key}. "
            f"The next step is `{step.name}`. Use this workflow context from "
            "the original input and prior handoffs:\n\n"
            …yaml-dumped scopes…
```

It exists precisely for this case - a fresh session that must not lose the run's history (orchestrator.py:713, 994).

**So the honest reading of Luke's condition is deliverable:** a new session, seeded with the finished run's handoffs and the transcript tail the modal is already showing. What is *not* deliverable is a fresh session that silently inherits the old brain transcript, and §6.3 says why.

### 6.2 The sharp edge: resuming a parked run skips the gate

This is the failure mode the design must be structurally unable to reach.

A suspended run records the step **after** the gate, not the gate:

```
orchestrator.py:867  (inside the await-step suspend block, 861-896)
                run.suspended_at_step = step_idx + 1
```

Resume feeds that straight back in as the starting step:

```
orchestrator.py:388,441  (resume_workflow, defined at 372)
    step_idx = run.suspended_at_step
    …
            interactive, start_step=step_idx, launch_model=launch_model,
```

and its own docstring says so in as many words:

```
orchestrator.py:379-383
    """Resume a suspended workflow from its await step.

    Restores the variable context and session, then continues execution
    from the step after the one that suspended.
    """
```

For this team's `issue-lifecycle`, the step after the approval gate is `implement`. Enumerated mechanically from the installed workflow rather than read off by eye:

```
$ python3 -c "import yaml;d=yaml.safe_load(open('<run>/package/workflows/issue-lifecycle.yaml'))
  [print(i, s['name'], '| await:', s.get('await','')) for i,s in enumerate(d['steps'])]"
0 setup            | await:
1 pickup           | await:
2 notify_start     | await:
3 route            | await:
4 spec             | await:
5 plan_review      | await:
6 await_approval   | await: approval
7 implement        | await:
8 pr               | await:
9 qa               | await:
10 notify_complete | await:
```

And every waiting run in this deployment carries `suspended_at_step: 7`, read from the live read model:

```
awaiting_action rows: 10
   wf-issue-lifecycle-eng-team-1016  approval  step 7  run_key 1016  resumable=True
   wf-issue-lifecycle-eng-team-987   approval  step 7  run_key 987   resumable=True
   wf-issue-lifecycle-eng-team-adhoc-e0dc9a95  approval  step 7  …
   (7 more)
```

**Step 7 is `implement`.** So a resume of #987 does not re-open the design conversation, does not re-run `plan_review`, and offers no rework path: it starts writing code against the unapproved spec.

Nothing downstream walks it back. `_run_workflow_async`'s loop simply begins at the given index (`step_idx = start_step`, orchestrator.py:751), and the workflow's only branch is forward - `route` at index 3 (`if: needs_spec == true` → `goto: spec`, `else: implement`), which is the sole `goto` in the file and never targets the gate.

Every path into that behaviour, printed and classified:

```
$ grep -rn "resume_workflow" --include=*.py bobi/ agents/
bobi/cli.py:2420:    from .workflow.orchestrator import resume_workflow
bobi/cli.py:2450:    success = resume_workflow(run, wf, timeout=timeout)
bobi/workflow/orchestrator.py:176:    resume path. Before wiring one up: resume_workflow re-stamps the registry
bobi/workflow/orchestrator.py:206:        target=resume_workflow,
bobi/workflow/orchestrator.py:291:    and asks for it here. ``resume_workflow`` deliberately never sets it.
bobi/workflow/orchestrator.py:372:def resume_workflow(
bobi/webapp/server.py:203:    def resume_workflow_run(name: str, run_id: str) -> JSONResponse:
bobi/webapp/run_actions.py:71:    (``try_resume_for_event``): ``resume_workflow`` re-stamps the session
```

| Hit | What it is |
|---|---|
| cli.py:2420, 2450 | `bobi agent <name> workflows resume <run_id>`. The **only live resume path** - `try_resume_for_event`'s own docstring says "No production caller today; the CLI ``workflows resume`` is the only live resume path" (orchestrator.py:175-176). |
| orchestrator.py:206 | `try_resume_for_event`'s thread target. Dormant, and its docstring blocks wiring it up until it spawns a process. |
| server.py:203-206 → run_actions.py:67 | `POST …/workflows/runs/{run_id}/resume`, which **spawns the CLI above** (run_actions.py:90-93). This is the one the webapp can reach. |
| orchestrator.py:176, 291, 372 | Definition and prose. |
| run_actions.py:71 | Prose. |

**The guard, and it is structural rather than advisory.** The page does not call the resume route today:

```
$ grep -n "resume" bobi/webapp/static/views/agent.js
(no hits)
```

`rowActions()` offers Transcript / Details / Close and nothing else (agent.js:609-640). The composer must not change that, and §8 pins it with a test that fails if `agent.js` ever gains a call to the resume route. The relay message in §6.4 additionally names the run id the manager must **not** resume, so the instruction and the absence of a button agree.

### 6.3 The finding: the transcript and the workflow step are one lever

Luke's condition needs "carry the conversation forward" without "resume the parked workflow step". Are the two separable? **Partly, and the boundary is exact.**

- **Separable:** carrying the run's *durable context* forward has nothing to do with `start_step`. §6.1's scopes are plain data; any fresh session can be handed them, and no resume is involved.
- **NOT separable:** carrying the *brain transcript* forward and re-entering that workflow are the same lever. The transcript is keyed by session name (sdk.py:657), and the name is a pure function of workflow, repo and run key:

```
orchestrator.py:227  (make_session_name)
    return f"wf-{workflow_name}-{repo_name}-{run_key}"
```

So reusing the transcript means reusing the name, which means re-entering that workflow - and there are exactly two entry points, neither of which is "at the gate":

| Entry | Start step | What it does to a parked run |
|---|---|---|
| `run_workflow` (a launch) | **0** - the default at orchestrator.py:483; `run_workflow` never passes one (orchestrator.py:340) | Re-runs `setup`, `pickup`, `notify_start`, `route`, `spec`, `plan_review` from the top, re-notifying as it goes |
| `resume_workflow` | `run.suspended_at_step` = the step after the gate | §6.2. Straight to `implement`. |

A relaunch is also admitted rather than refused, because `waiting` is not in the admission set:

```
subagent.py:1128  (inside the launch admission lock)
        if existing and existing.status in ("starting", "running", "idle"):
```

and when the relaunched run reaches the gate again it mints a **second** run record (`WorkflowRun.create` at orchestrator.py:865; `run_id=str(uuid.uuid4())[:8]` at state.py:209), leaving the original parked forever. Two waiting rows for one ticket.

**That is the finding, stated plainly: there is no way to re-enter a parked workflow at its gate.** So "picks up where it left off" is specified as context carried in a new session's prompt, not as a resumed transcript - and the design gains a property it would otherwise lack, namely that it cannot touch the parked run at all.

One more reason not to lean on the transcript: it is not durable across a reinstall. `check_image_rotation` (sdk.py:97-115, clearing at 114) wipes the saved id when the installed image hash changes, so a run parked across an upgrade has already lost its resume token while its handoffs are still on disk.

### 6.4 What actually happens when the operator types

The target row is non-live (`detail.live === false`, §5.3). The composer stays **enabled**, and its label says what sending will do: this session has finished, sending starts a new one that picks up its context.

On submit, the frontend posts to the endpoint that already ships - **the same `/chat` call as the live branch**, with an empty `subagent`:

```
POST /api/agents/{name}/chat   {"subagent": "", "text": "<relay message>"}
```

An empty `subagent` means "the team manager", which the hosted runtime already implements:

```
admin.py:507  (inside _dispatch_chat_async, defined at 494)
                target = session or self._manager_session()
```

The manager is live and addressable. Probed on this deployment:

```
manager_session_name          -> bobi-eng-team-director
list_active()                 -> [('wf-adhoc-…-987-spec-rev4-fresh-session', 'running'),
                                  ('bobi-eng-team-director', 'idle')]
```

**The one backend line.** `LocalRuntime.chat_submit` does not honour the empty session the route explicitly permits (server.py:358 reads `subagent` as optional - only `text` is required, server.py:360); it passes `""` straight through:

```
runtime.py:698  (inside chat_submit's worker, defined at 687)
                service.ask(root, session, text,
```

and `service.ask` rejects it on its membership guard (service.py:804). Probed, from the worktree under test - the guard runs before any delivery, so nothing was sent:

```
LocalRuntime path, empty session -> MessageDeliveryError: unknown agent ''
supervisor path,   empty session -> bobi-eng-team-director
```

That divergence is pre-existing and is a defect on its own terms: one route, two runtimes, opposite behaviour on an input the route documents as optional. The fix is to resolve an empty session to `service.manager_session_name(root)` exactly as admin.py:507 does - **one line and a test** - and it is what makes the continuation branch work identically on `bobi app` and on the hosted fleet.

**The relay message.** The frontend composes it from data the row already carries: a workflow row's `detail` holds `await_event`, `suspended_at_step` and `run_key` (runs.py:222-226), and `session_id` is the session name (runs.py:217). No read-model widening. Shape:

```
[bobi console] The operator typed this on a finished run's transcript.

target_session: wf-issue-lifecycle-eng-team-987
run_key:        987          workflow: issue-lifecycle
state:          waiting at `await_approval`, run 11d31ce5

<the operator's text, verbatim>

Start a FRESH session to carry this forward. Do NOT resume run 11d31ce5 -
resuming a parked run starts at the step AFTER the gate.
```

**Which run key, fresh or resumed.** A **new** run key on `-w adhoc`, never the parked one, for the reasons in §6.3: relaunching `-w issue-lifecycle --run-key 987` restarts at step 0 and forks a second waiting run. `adhoc.yaml` is a single prompt step (`prompt: "${{input.task}}"`), so the seeded task *is* the context.

**What is seeded.** The relay message above, plus whatever the manager reads from durable state - the run's handoffs under `state/sessions/<session>/` (four of them for #987: setup, pickup, spec, plan_review) and the run record's scopes (§6.1). All of it is on disk and none of it needs the dead process.

**What the operator sees.**

1. Send disables the control, as `closeRun()` already does (agent.js:643-662).
2. The submit-then-poll cycle is unchanged - `/chat` then `GET …/chat/{message_id}` until `status !== "pending"`.
3. On `done`: an inline confirmation naming what was sent and to whom. **Not** a re-fetch of this row's transcript - the reply landed in the manager's transcript, not this dead session's, and re-rendering an unchanged transcript would read as the message having been swallowed. The new run appears in the table on its own; `pollRuns` is already on a 4s timer (agent.js:865-867).
4. On `error`: `job.error` inline via `slabError` (agent.js:707-710), same as the live branch.

### 6.5 What this costs, honestly

**On existing machinery: yes, with one line.**

| | Cost |
|---|---|
| New endpoints | none - `/chat` and its poll ship on both runtimes |
| `ADMIN_COMMANDS` edits | none - `chat` is already in both halves |
| Engine / CLI / Slack | none |
| Backend | **1 line + 1 test** (runtime.py:687-708, empty session → manager), on top of revision 3's `detail.live` |
| Frontend | the non-live branch of the composer: label, relay message, confirmation rendering |
| Read model | nothing new - `run_key` and `await_event` are already in `detail` (runs.py:222-226) |

**And the one thing it cannot promise.** The manager is an agent, not a syscall. The console *requests* a fresh session; it does not spawn one. Luke's word is "can", and this satisfies "can" - the capability is real, the manager is live, and dispatching subagents is its ordinary job - but a spec should not describe a delegated request as a guaranteed spawn, so: **if the manager is stopped, or judges the request differently, no session starts.** The `error` job surfaces a stopped manager honestly (`ask` refuses an address that is not live); a manager that simply chooses otherwise surfaces as a reply, not an error.

**The deterministic alternative, and its real price.** A dedicated `continue_run` write action would guarantee the spawn: `run_actions.py`, both `TeamRuntime` implementations, a `server.py` route, a supervisor handler, and **both `ADMIN_COMMANDS` halves** (fleet.ts:350-368 and admin.py:55-61, pinned together by `tests/test_admin_command_parity.py`), plus the frontend. Roughly 7 files, and it is the exact shape revision 3 deleted on Luke's direction. It is not proposed here; it is offered as decision 5 (§10) so the choice between determinism and machinery is made deliberately rather than by omission.

---

## 7. Scope

**In**

- `bobi/webapp/static/views/agent.js`: the composer in the transcript branch, both branches (live → deliver, non-live → continue, §6.4), the submit-then-poll cycle, the post-reply transcript re-fetch on the live branch.
- `bobi/webapp/static/app.css`: composer styling, extending the surviving `.chat*` block (app.css:601-610).
- `bobi/webapp/runs.py`: the single additive `detail.live` field (§5.3).
- `bobi/webapp/runtime.py`: the one line that resolves an empty `subagent` to the manager, matching admin.py:507 (§6.4).
- `tests/`: §8.
- Docs: `docs/RUN_DRILLDOWNS.md` (the slab gains a write surface), `docs/RUNS_VIEW.md` (the new `detail` field).
- `plans/2026-07-31-single-agent-view.md`: dated amendment recording the reply surface (§3.5), subject to decision 3.

**Out**

- **Anything on the resume path.** The fire-and-forget spawn whose failures read as success, the torn-claim orphan, and the reminder step's "Reply in this thread to continue" promise are all real and all pre-existing. They belong to resume, which this design does not touch. Recorded here so they are not lost, and deliberately not bundled.
- **Resuming a parked workflow run, by any route.** §6.2. The page has no resume call today and gains none; §8 pins that with a test.
- **A `continue_run` write action.** The deterministic alternative to §6.4, priced in §6.5, deferred to decision 5.
- Making a suspended gate answerable *as a chat target*. §4: no live process exists. That is a workflow-engine question, and the engine is frozen. §6 gives that row a continuation instead, which is a different thing.
- Relaxing `ask`'s membership guard (service.py:803-804). It would change nothing (§4.2) and it exists on purpose. Resolving an *empty* session to the manager (§6.4) is not this: it addresses a live manager through the same guard rather than around it.
- `try_resume_for_event`, any approve/reject vocabulary, any Slack path.
- A persistent chat panel. The plan removed the column (§3.5); this is a composer inside a modal.
- `VERSION`, `pyproject.toml` version, `CHANGELOG.md`. Untouched.

---

## 8. Verification plan

The risk is a box that accepts typing and delivers nothing, so the tests must prove **the message reached the agent**, not that a POST returned 200.

**Unit (Python)**

- `build_runs` stamps `detail.live` true for a session in `ACTIVE_STATUSES` and false for a `waiting` gate row whose pid is dead. This is the guard that picks the composer's branch, and it is the one test that must exist.
- `detail.live` is false for a `waiting` workflow row **even though its status renders as `idle`**. This pins the §5.3 ambiguity directly.
- `LocalRuntime.chat_submit` with an empty `subagent` resolves to the manager session and does **not** raise `unknown agent ''`. This is the §6.4 line, and the test is the proof the two runtimes now agree - assert it against the same name `service.manager_session_name` returns.

**Frontend, executed as real JavaScript under Node**

The repo already had this harness and it is not Playwright (the quoted
exemplar, `tests/test_webapp_markdown.py`, was deleted 2026-08-20 with the
orphaned renderer it tested — #819; `tests/test_webapp_composer.py` is the
surviving example of the pattern):

```
tests/test_webapp_markdown.py:1, 6-7
"""The webapp's agent-reply markdown renderer, executed as real JavaScript.
...
Asserting on the JS source text would prove nothing about what it renders, so
these run the actual module under Node and parse what comes back.
```

Following that pattern:

- The composer renders on both branches, and its label differs: `detail.live` true → reply, false → continue in a new session.
- Live branch: submit posts `{subagent: <session>, text}` to `/chat` and disables the control.
- **Non-live branch: submit posts `subagent: ""`** - the manager - and the text contains the target session name, the run key and the run id (§6.4). Assert on the composed payload, since that payload is the whole contract with the manager.
- A `done` job re-fetches the transcript on the live branch and does **not** on the non-live one (§6.4, point 3).
- An `error` job renders `job.error` inline, and the control re-enables.
- **The guard: `agent.js` contains no call to the resume route.** A grep-shaped assertion over the module source, and the one test whose failure means the sharp edge (§6.2) has been wired in.

**Integration (isolated `BOBI_HOME`, real sessions)**

- Against a **live** session: POST `/chat`, poll the job to `done`, and assert the text arrived as a turn in that session's transcript. This is the test that proves the feature.
- Against a **suspended gate's** session: assert delivery to that session is refused with `process is dead` rather than silently accepted. This pins §4 as a regression guard, so a future change that points the reply branch at gate rows fails here.
- **The continuation branch, end to end**: with a real manager session running, POST `/chat` with an empty `subagent` and assert the job resolves `done` and the message is in the **manager's** transcript. That proves the relay is delivered; whether the manager then launches is the manager's judgement (§6.5), so the test asserts delivery and not the spawn.
- **Anti-regression on the parked run**: after the above, assert the target run's `status`, `suspended_at_step` and `run_id` are byte-identical to before. This is the test that would fail if anything in this feature ever reached `resume_workflow`.

**Brain**: a real-Claude leg is warranted. Per CLAUDE.md's "one mechanism, two brains" rule, this is event delivery through a live session and a turn taken by the brain, which is the case the rule names rather than the brain-agnostic control-plane case. Parametrize `[stub]+[claude]`, gate the claude leg on the CLI.

**Manual capture**: drive the real page against a live session and attach a GIF of type → send → the reply appearing in the slab, per the house proof-of-work rule.

---

## 9. Implementation plan

Tests first, each step independently reviewable.

1. `runs.py` `detail.live`, with its two unit tests red first.
2. `runtime.py` empty-session → manager (§6.4), with its unit test red first.
3. Node-executed JS tests from §8, red, including the no-resume-route guard.
4. The composer in `agent.js` plus `app.css`: both branches, submit-then-poll, live-branch re-fetch, non-live relay message, inline error.
5. Integration tests: live session delivers, gate refuses, relay reaches the manager, parked run unchanged.
6. Docs (§7) and the dated plan amendment (decision 3).
7. Review gate, full suite, frontend capture, PR.

---

## 10. Decisions reserved for Gate 1

> **Resolved 2026-08-18.** Luke approved the spec as written, which selects
> the primary option on each decision below. Recorded here so the build is
> auditable against what was approved rather than against a reading of it.
>
> | # | Resolution | Where it landed |
> |---|---|---|
> | 1 | Accepted. The reply branch serves live sessions; the gate row gets §6's continuation. Luke had already accepted the scope shift in Slack, conditioned on §6. | `renderComposer` branches on `detail.live` |
> | 2 | Accepted: the additive `detail.live` field. The rejected client-side derivation was not built. | `runs.py` `build_runs`, `docs/RUNS_VIEW.md` |
> | 3 | Confirmed: a dated amendment, not a silent rewrite. | `plans/2026-07-31-single-agent-view.md`, 2026-08-18 entry |
> | 4 | Confirmed: the resume path's pre-existing defects stay unbundled. Nothing in §7's Out list was touched. | - |
> | 5 | The **relay** through the live manager, which was the recommendation and the only option that is genuinely existing machinery. `continue_run` was **not** built. | `continuationRelay` + the one `runtime.py` line |

1. **Accept that the *reply* branch serves live sessions, not awaiting-action gates.**
   The issue's title asks for a box on the awaiting-action slab. §4 shows there is no live agent behind those rows and the terminal cannot reach them either, so the reply branch appears on live sessions. §6 gives the gate row a continuation branch instead, which is Luke's condition and not a chat.
   That lands on Linear MOD-372's own title ("chat with a **running** agent") and is narrower than GitHub #987's. It should be accepted knowingly, and #987's title updated to match.
2. **Accept the one additive read-model field** (`detail.live`, §5.3), or direct me to derive liveness client-side and take the fragility.
3. **Confirm the dated plan amendment** (§3.5). The Locked plan removed the chat column with a stated reason; this puts a composer back. My reading is that an amendment is the right mechanism and the composer is narrow enough to be uncontroversial. If you disagree, say whether the objection is to the surface or to the amendment.
4. **Confirm the out-of-scope list** (§7), in particular that the resume path's pre-existing defects stay unbundled.
5. **NEW - choose how the continuation is started (§6.5).**
   The specified design relays through the live manager: **1 line of backend**, no new verb, and the spawn is a *request* an agent fulfils.
   The alternative is a dedicated `continue_run` action: the spawn is guaranteed, and it costs ~7 files including **both `ADMIN_COMMANDS` halves** - the shape revision 3 deleted on your direction.
   My recommendation is the relay, because your standing direction is "existing machinery" and the relay is the only option that genuinely is. Say if you want determinism instead; it is a real trade, not a formality.

No code will be written until these are answered. **Luke's Slack message is a condition on the design, not approval of it.**

---

## 11. Revision record

### Revision 5 - 2026-08-19, Zach's rejection

Zach reviewed the built continuation branch and rejected it: parked sessions
should be resumed. He approved the refactor, scoped to four changes with none
in the orchestrator's step loop. §12 is the design; §6 is superseded but kept
for its findings.

The three sharp edges the investigation flagged
(`state/sessions/wf-adhoc-eng-team-1016-resume-semantics`) were each handled
rather than noted: the empty-verdict resolution decides the route's direction,
the `else` is explicit, and the re-suspend bookkeeping is fixed because a
rejection reaches it every time. §12.3 has the detail.

**Not obtained:** a cross-model second opinion. `codex` is unauthenticated in
these containers.

### Build - 2026-08-18, Gate 1 granted

Luke approved revision 4 and asked for it to be tested. Built as specified;
the design below is unchanged, so this section records only what shipped and
where the build learned something the spec did not say.

**Landed** (`agent/987`, this PR): the composer and its two branches in
`agent.js`, its slab-surface styling in `app.css`, the additive `detail.live`
in `runs.py`, and the one `runtime.py` line resolving an empty `subagent` to
the manager. Plus docs: `RUN_DRILLDOWNS.md` (the slab's write surface),
`RUNS_VIEW.md` (`detail.live`), and the dated plan amendment §3.5 called for.

**One structural change to §9's plan, and the reason for it.** The relay
message moved out of `agent.js` into its own module,
`webapp/static/views/composer.js`. `agent.js` imports `shell.js`, which reads
the page token from a meta tag at module scope, so the view cannot be imported
under Node without faking a DOM - and faking one to test a pure string
function proves nothing about the string. This is the same split
`markdown.js` had in this directory at the time (deleted 2026-08-20 as dead
code, #819), for the same reason: the relay IS the contract with the
manager, and it is only testable while it is importable.

**Verification, against §8.** Every listed test exists.
`tests/test_webapp_runs.py::TestLiveness` covers the `detail.live` fold
including the `waiting`-renders-as-`idle` ambiguity;
`tests/test_webapp_server.py` covers the empty-`subagent` line against the
name `service.manager_session_name` returns;
`tests/test_webapp_composer.py` runs the relay under Node and pins the
absence of a resume call; `tests/e2e/test_webapp_ui.py::TestComposer` drives
the real page in Chromium for both branches, the payloads, the re-fetch
asymmetry, and the inline error; and
`tests/integration/test_webapp_chat_delivery.py` proves delivery against real
sessions on both brains - the reply reaches a live session's transcript, a
suspended gate refuses it rather than swallowing it, the relay reaches a live
manager, and the parked run record is byte-identical afterwards.

**The `[stub]+[claude]` judgement, made rather than inherited.** §8 called a
real-Claude leg warranted, and the build agrees: what is being proven is that
a message typed in a browser becomes a *turn taken by a brain*, which is the
case CLAUDE.md's rule names. Both legs run and both pass.

### Revision 4 - 2026-08-18, Luke's condition

Luke accepted revision 3's scope shift conditionally: *"I think that's okay, as long as the user's input on that modal can create a fresh session that picks up where the last one left off"* (Slack, 2026-08-18). That condition is now §6.

**It is achievable on existing machinery, and the honest price is one line** (§6.5): `/chat` with an empty `subagent` already means "the manager" on the hosted runtime (admin.py:507) and errors on the local one (probed: `MessageDeliveryError: unknown agent ''`). Closing that divergence is the whole backend delta.

**The finding that shaped the section** is §6.3: carrying the *conversation* forward and resuming the *parked workflow step* are separable, but carrying the brain **transcript** forward and re-entering the workflow are one lever. The transcript is keyed by session name (sdk.py:657) and the name is a pure function of workflow/repo/run key (orchestrator.py:227), so the only two entry points are step 0 (a launch) and the step after the gate (a resume). **There is no way to re-enter a parked workflow at its gate.** So "picks up where it left off" is specified as durable context in a fresh session's prompt - the run's persisted handoffs, which `_continuation_prompt` (orchestrator.py:553-565) already exists to carry - not as a resumed transcript.

**The sharp edge, verified rather than assumed** (§6.2): `run.suspended_at_step = step_idx + 1` (orchestrator.py:867) and `start_step=step_idx` on resume (orchestrator.py:441). For `issue-lifecycle` that step is index 7, `implement`, and all 10 waiting runs in this deployment carry `suspended_at_step: 7`. A naive resume from the modal would start writing code against an unapproved spec with no rework path. The design cannot reach it: the page has no resume call today (`grep -n "resume" agent.js` → no hits), gains none, and §8 pins that with a test.

**Length discipline.** Revision 3 was 567 lines and got there by deleting machinery. Revision 4 adds one section and touches §1, §5.4, §7, §8, §9 and §10. Nothing from revisions 1-2 is revived.

**Verification.** Every citation and probe in §6 was derived first-hand at `agent/987` @ `99f5e23`, with `bobi` imported from that worktree, never from the parked repo checkout. Inventories print their grep and classify every hit.

**Not obtained:** a cross-model second opinion. `codex` is unauthenticated in these containers; no cross-model result is invented here.

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

---

## 12. Revision 5 - the gate is answered, not relayed

Zach reviewed what revision 4 built and rejected its continuation branch:
parked sessions should be resumed. He approved this refactor. What follows is
the design as scoped, the three sharp edges it had to handle, and what was
deleted.

### 12.1 What was wrong with the relay

Nothing in the relay was broken. It delivered, it was tested, and §6.5 was
honest about what it could not promise. It was the wrong shape.

An operator looking at an awaiting-action row is looking at a run that asked
them a question. The relay answered by describing the question to a third
agent and asking it to start over somewhere else - losing the transcript, the
step position, and the run identity, and adding a hop that can decide not to
act. The run stayed parked either way.

§6.2 is why it was built that way: a resume starts the step AFTER the gate, so
resuming `issue-lifecycle` at index 7 runs `implement` against an unapproved
spec. Revision 4 treated that as a wall and routed around it. It is not a wall.
It is where the answer goes.

### 12.2 The design

Four changes, none in the orchestrator's step loop.

**A1. The verdict reaches the engine.** `workflows resume` takes `--verdict`
(`approve` | `reject`) and `--reply`, and passes them as
`resume_workflow(..., event=...)`. That parameter already existed and its
`data` already becomes the run's `event` scope
(`orchestrator.py`, `ctx.set_scope("event", ...)`); nothing about the inlet is
new, it was simply never populated. The vocabulary is
`bobi.workflow.schema.GATE_VERDICTS`, so the CLI's `click.Choice` and the
webapp's validation cannot drift apart.

**A2. The console carries it.** `run_actions.resume_run` takes `verdict` /
`reply`, appends them to the command it spawns, and refuses a verdict outside
the vocabulary before spawning - the child's output goes to `/dev/null`, so a
refusal down there would look like an accepted resume that did nothing. Both
runtimes and the supervisor's `resume_run` admin command carry the two args;
they are additive, so an older console that sends only a `run_id` still
resumes, with no verdict.

**A3. The composer answers instead of relaying.** Its three branches are now
live / gate / ended (`composerMode` in `composer.js`). A gate row gets Approve
and Reject, which POST `{verdict, reply}` to the resume route. A row that is
neither live nor a gate gets a sentence and no control, because there is
nothing for a control to do.

**A4. The workflow reads it.** `issue-lifecycle` gains one route step
immediately after `await_approval`:

```yaml
  - name: approval_route
    if: "${{event.verdict}} == 'approve'"
    goto: implement
    else: spec
```

The `+1` the engine already writes lands on this route, so an approve
continues exactly as it did before, and a reject goes back to `spec` - which
re-enters `plan_review` and the gate, on the same run and in the same session.
The `spec` step's prompt now carries `${{event.verdict}}` and
`${{event.reply}}`, so a rework can see what it is reworking.

### 12.3 The three sharp edges

**F1. A missing scope resolves to empty, quietly.** `variables.py` resolves an
unknown scope or key to `""` with a log warning and nothing else, so an
unpopulated verdict evaluates the condition false and takes the `else`.
**The route therefore tests for the verdict that ADVANCES**, making `spec` -
the safe outcome - the `else`. Written the other way round
(`if reject → spec, else implement`), an empty verdict falls into `implement`,
which is the exact failure this change exists to remove. Covered by
`TestGateVerdictRouting::test_an_unanswered_or_malformed_verdict_never_advances`
over six shapes of absent and malformed verdict, and by the shipped-workflow
assertions in `TestApprovalGateRouting`.

**F2. A route with no `else` falls through to `step_idx + 1`.** At this
position that is `implement`. The `else` is written out, and
`test_a_route_with_no_else_would_fall_through_to_implement` asserts the engine
behaviour the workflow is written around - so a future reader who deletes the
`else` as redundant has a failing test explaining what it was holding back.

**F3. A re-suspend used to be stamped `completed`.** Every suspend mints a new
`run_id`, and `resume_workflow` read `_run_workflow_async`'s bare `True` as
"finished" - so a resumed run that parked on a later await was recorded as
completed while a fresh record waited.

This path is now reached on **every rejection**, so it could not be left
alone: reject → rework → re-gate is a re-suspend by construction, and the
first one would have reported the run finished. `_run_workflow_async` now
returns an explicit outcome (`OUTCOME_COMPLETED` / `_FAILED` / `_SUSPENDED`),
`run_workflow` emits no terminal event on a suspend, and `resume_workflow`
stamps `superseded`.

That is the orchestrator half of the fix. The CLI half is
[399e0b0](https://github.com/moda-labs/bobi-agent/commit/399e0b0) from
`agent/818-lane-a`, cherry-picked here, which stops `workflows resume` from
printing "Workflow completed." for it. **399e0b0 alone would have been
inert**: the `superseded` stamp it branches on lives in `d40e4b6` on that same
branch, a nine-defect commit whose other eight fixes are unrelated to this
work. Only the stamp came across; the rest stays where it is.

### 12.4 What was deleted

A migration deletes the path it replaces.

- `composer.js`'s `continuationRelay` and every test of the relay message.
- `TestNoResumeCall`'s three source-level guards, which asserted that no view
  contains the string `/resume`.
- `TestComposer::test_the_composer_never_reaches_the_resume_route`, the
  behavioural version of the same guard.
- `test_the_parked_run_is_byte_identical_afterwards`, which asserted that
  nothing in this feature may reach the resume path.

All four existed because resume could not be trusted. Trusting it is the
change.

One thing from revision 4 stays: the `runtime.py` line resolving an empty
`subagent` to the manager. It is runtime parity - the route documents
`subagent` as optional and `EventBusRuntime` has always read it that way - not
part of the relay. Its integration test is renamed to say so.

### 12.5 Verification

| what | where |
|---|---|
| which step each verdict runs | `tests/test_orchestrator.py::TestGateVerdictRouting` - 11 tests over the real step loop, route evaluation and run records |
| the same three endings through the real CLI subprocess | `tests/integration/test_gate_verdict_resume.py` - a real suspended run on the stub brain, resumed by the real command |
| the verdict surviving every hop | `tests/test_webapp_resume.py::TestVerdictReachesTheSpawn`, `tests/test_cli.py::TestWorkflowResume`, `tests/test_hosted_single_agent_view.py` |
| the branch and the payload | `tests/test_webapp_composer.py` under Node |
| the page, in a real browser | `tests/e2e/test_webapp_ui.py::TestComposer` |
| the shipped workflow's gate shape | `tests/test_eng_team_role_constraints.py::TestApprovalGateRouting` |

Five mutants were run and each was killed by the test that claims the
behaviour: flipping the shipped route to the unsafe direction; stamping a
re-suspended run `completed`; dropping `--verdict` from the spawned command;
dropping `event=` from the CLI's resume call; and making `composerMode` treat
a gate as live.

**Not obtained:** a cross-model second opinion. `codex` is unauthenticated in
these containers; no cross-model result is invented here.
