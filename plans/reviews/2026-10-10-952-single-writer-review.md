# Codex review of the #952 single-writer rewrite

Reviewer: OpenAI Codex (`codex exec -s read-only`, `model_reasoning_effort="high"`), 2026-10-10.
Spec reviewed: `plans/2026-10-09-workspace-event-subscriptions.md` as rewritten to the single-writer
design, 622 lines, before this review was folded.
Citations in the review were checked against `origin/main` `f712372deb832e8d0817d606dcb17e849143d616`.

Verbatim report follows.

---

REQUEST CHANGES

### BLOCKER F1 - The boot overwrite race still exists

- Spec: “With the composition inside the session there is no pre-session snapshot to go stale” and a reload before registration “gets a `409` ... after which the boot composes from the file anyway.”
- Code: The health server starts before the session at `bobi/service.py:687-691`. `Session` calls `_start_event_subscription` at `bobi/session.py:1673-1674`, but the initial register/PUT happens inside that call at `bobi/subagent.py:1957-1960`, before it returns at `bobi/subagent.py:2016`.
- Failure: Boot composes `A`; CLI writes `A+X`; reload gets `409`; boot then registers/PUTs cached `A`. Nothing schedules another reload. Moving composition from `service.py` to the top of `_start_event_subscription` only narrows the race.
- Change: Make a reload controller discoverable before the initial compose/apply, or record a pending reload on `409` and force a final locked recompose after registration. Test 7 must block after the first composition but before the boot write.

### BLOCKER F2 - The composition contract deletes supported worker subscriptions

- Spec: “only when `session == manager_session_name(project_path)` ... appends ... `extra`.” Test 11 contradicts this by expecting `["inbox/<name>"] + extra` for a non-manager.
- Code: Persistent subagents explicitly support `--subscribe` at `bobi/cli.py:3473-3476`; `run_persistent_agent` documents those as additional topics at `bobi/subagent.py:840-841`; every `Session` currently retains them at `bobi/session.py:339-341` and appends them at `bobi/session.py:1657-1660`.
- Change: Every session must compose `inbox/<self> + extra`. Only workspace, monitor, and lifecycle layers should be manager-only. Add coverage for a persistent worker retaining its explicit external topic while never inheriting manager topics.

### BLOCKER F3 - A direct workspace edit can recreate the cross-session inbox leak

- Spec: The strict validator rejects schema faults but not `inbox/` or `reply/`; only CLI arguments reject those namespaces. The spec also says direct edits apply on reload/start.
- Code: The server treats `inbox/*` and `reply/*` as ordinary bubble-scoped topics at `event-server/core/src/core.ts:406-414`; registration accepts any non-empty string at `event-server/core/src/core.ts:1394-1401`. Current composition appends arbitrary supplied topics at `bobi/session.py:1657-1660`.
- Failure: An agent or human can write `inbox/<worker>` directly into `workspace/subscriptions.yaml`; the next reload routes that worker’s private messages to the manager.
- Change: Put the namespace ban in `workspace_subscriptions`, shared with CLI validation. Add a direct-file test proving reload/start performs no PUT for either forbidden namespace.

### MAJOR F4 - A 403 cannot be diagnosed as eviction

- Spec: `{"error":"deployment_unknown","http_status":403}` and the CLI should “name eviction as the cause.”
- Code: Update authentication returns the same 403 for any missing deployment, wrong deployment ID, or wrong API key at `event-server/core/src/core.ts:1491-1492`. `authenticateDeployment` deliberately collapses those cases at `event-server/core/src/core.ts:783-790`. Current client wording correctly says missing/expired or invalid credentials at `bobi/subagent.py:1901-1905`.
- Change: Use a neutral error such as `deployment_auth_failed` and preserve the current ambiguity. Test both an evicted deployment and a corrupted key; they must produce the same diagnosis unless the server contract changes.

### MAJOR F5 - The reload request can outlive both the handler and CLI timeouts

- Spec: “Every network call inside it is bounded” and the health handler timeout is presented as protection.
- Code: `_HANDLER_TIMEOUT` is only applied to socket request reads at `bobi/manager_health.py:70-73`; it does not deadline handler execution. Authorization loops serially over topics at `bobi/events/server.py:740-784`, with up to 10 seconds per call at `bobi/events/server.py:650-654`. The shared client default is 10 seconds at `bobi/http.py:33`.
- Failure: One slow authorization can make the CLI’s POST time out while the manager continues authorizing and later applies the change. The caller then cannot know whether the change became live.
- Change: Define an overall reload deadline and a matching longer CLI timeout, or make reload asynchronous with a result ID. Add a slow-authorization test that proves the command never reports an indeterminate result while the handler later mutates routing.

### MAJOR F6 - “Last accepted” state and named diffs have no defined source

- Spec: `active_subscriptions` is deleted; `GET /subscriptions` returns the last accepted list and timestamp; the CLI prints every added and removed topic.
- Code: `_put_subscriptions` currently returns nothing at `bobi/subagent.py:1926-1946`. The event server returns only the new list plus numeric `added` and `removed` counts at `event-server/core/src/core.ts:1537-1540`.
- Failure: The proposed POST wire shape has no named diff, and deleting the only accepted-list cache leaves no defined source for `GET` or `at`.
- Change: Specify a lock-protected in-memory accepted record containing subscriptions and acceptance time, initialized from successful register/PUT responses. Return `added_topics` and `removed_topics`, or define another race-safe way for the CLI to obtain them.

### MAJOR F7 - R4 is too cheap and too plausible to accept

- Spec: A reconnect may narrow routing after transient auto-detection failure; covering it costs “about four lines plus a new return signal.”
- Code: Slack and Linear detection swallow failures into `[]` at `bobi/events/adapters.py:201-205` and `:297-315`; discovery then returns a partial set or falls back at `bobi/events/subscriptions.py:68-78`.
- Change: Do not PUT a degraded auto-detection result during reconnect. Carry detection completeness, or retain the last accepted set when detection is incomplete. The accepted-state record already required by D4 makes this inexpensive. Add a reconnect test with Slack/Linear timing out.

### MAJOR F8 - The load-bearing reload lock has no adversarial test

- Spec: “Boot, reload, reconnect ... under one `threading.Lock`.”
- Code: Deaf resubscription runs on its own thread at `bobi/events/client.py:440-446`; health requests run on independent daemon request threads at `bobi/manager_health.py:36-51`.
- Gap: Test 4 proves the file lock. Test 9 is sequential. Neither proves that an old route composition paused before PUT cannot land after a newer reconnect composition.
- Change: Add a deterministic barrier test: pause reload A after composing, mutate the file, start deaf reload B, release A, and assert the final server set is the newest file contents.

### MINOR F9 - The global single-writer statement is factually false

- Spec: “The MANAGER process is the only thing that ever PUTs `/deployments/<id>/subscriptions`.”
- Code: Every session with saved state PUTs during startup at `bobi/subagent.py:1957-1960`, and every session can PUT on deaf reconnect at `bobi/subagent.py:1974-1982`.
- Change: State the actual invariant: one owning session process per deployment. The manager instance lock supports that for the manager deployment; workers still write their own deployments.

### MINOR F10 - An existing test break is omitted

- Spec: `session.py` stops prepending the inbox before calling `_start_event_subscription`.
- Code: `tests/test_session.py:907` asserts the background retry receives `["inbox/test-wake", "github:o/r"]`; it will receive only the raw extras after this move.
- Change: Name and update this characterization test as part of the composition migration.
