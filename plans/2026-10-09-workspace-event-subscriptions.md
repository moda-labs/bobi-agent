# Workspace-owned event subscriptions

Issue: [#952](https://github.com/moda-labs/bobi-agent/issues/952)
Status: spec, awaiting approval (Gate 1).

Every `bobi/`, `event-server/`, `tests/`, `docs/` and `AGENTS.md` file:line below was read at `70db2e1059fdf8f35751b17806496cb91f427b09` (`origin/main`, 2026-10-09).
Every `moda-agents` file:line was read at `bf9d1088ad08bafd175232daeddcce3ab2d7e76c` (`origin/main`, 2026-10-08).
One coordinate system, no translation table.

Seven review rounds preceded this document and their reports are committed next to it in `plans/reviews/`.
Rounds 1 to 4 are same-model with fresh context; rounds 5, 6 and 7 are OpenAI Codex.
This revision is a rewrite, not a seventh fold: round 6 found that six rounds of insertion-only amendment had left six contradictory instruction pairs and that every round kept finding the same class of defect (an unmodeled writer, or an unmodeled stale-state boundary) because the spec had no single distributed-state section.
That section is now the centre of the document, and the superseded text is deleted rather than layered over.

Round 7 reviewed the rewrite and found eight defects, all verified against real code and all folded here rather than appended: the server's own eviction sweep is a fifth writer that deletes the identity the other four write through (so I5 carries a proviso and the recovery is Q5); a hand edit of the workspace file is a sixth writer this design cannot serialize; a CLI delta computed outside the record lock loses one of two concurrent topics; the boot publishes its pid before it applies its subscription snapshot, so liveness alone was the wrong selector; an unlocked record write voids the generation counter as a compare-and-swap token; derived-versus-declared ownership is time-varying, so permanent disjointness was not holdable; the CLI could write a topic its own validator rejects; and one test was green before the change.
It found no citation error.

## Problem

Changing which repos a running team hears about means editing agent pack source in a second repository, rebuilding the pack, reinstalling it, and restarting the manager.

The topics live in `subscribe:` in `moda-labs/moda-agents` at `agents/moda-eng-team/agent.yaml:127-134`, installed as a frozen read-only image at `<run>/package/agent.yaml` (`bobi/runtime_guard.py:143-153`).
The 2026-08-03 familystories-ai offboard is the measured case: a commit in another repo, a pack rebuild, an install, and a restart.
The pack comment at `agents/moda-eng-team/agent.yaml:124-126` says so itself: "the server-side subscription set is replaced by `register` on the next manager-session start, so this team must be redeployed for the cut to take effect."

A team cannot change its own subscriptions, so onboarding is gated on a human doing a release in a different repository.

## Decisions

All settled by Zach in Slack `C0BAEN48KQR` on 2026-10-09, not reopened here.
D1 to D5 are from thread `1791519568.830069`; D6 to D9 answer the questions this spec posed back, in thread `1791568791.200569`.

**D1.** The core question is generic: a running team may mutate its own event subscriptions.

**D2.** Subscriptions live as a list in the WORKSPACE; the pack `agent.yaml` `subscribe:` is only the SEED.

**D3.** A generic framework CLI subcommand applies changes, e.g. `bobi agent <name> subscriptions list|add|remove` (topics, not repos: no repo concepts in `bobi/`, and no `repos` CLI, per his August rulings).
It persists the workspace list and hot-applies via the existing identity-preserving `PUT /deployments/<id>/subscriptions {"replace":[...]}`.
No restart needed.

**D4.** Read-back of what is live, as relaxed by D8: `subscriptions list` shows the local last-accepted list, labelled "last accepted at T".
Not via the admin channel.

**D5.** `managed_repos` is eng-team specific: remove it from the pack `agent.yaml`, move it into a director-maintained workspace file, and update the eng-team director ROLE prompt to read and keep it current.
Prompt and pack change only, no bobi-agent code.
Accepted trade-off: the director can grant itself branch-delete authority (Zach, August: branches are recoverable).

**D6.** `workspace/subscriptions.yaml` is created lazily, by the CLI, on the first `add` or `remove`.
Nothing seeds it at boot and no pack template ships it.

**D7.** When the server rejects the live apply, the CLI keeps the workspace change, reports the server's exact `unauthorized_topics` list, and exits non-zero.

**D8.** No new GET endpoint, and server introspection stays out of scope.
`list` shows the local last-accepted list, labelled "last accepted at T".
The existing reconnect re-assert remains the repair for a stale server-side index.

**D9.** The event server's non-atomic `replace` is accepted as a known failure mode.
This spec records the acceptance and states how an operator detects and recovers from a partial replace.

The old generic-overlay design (`overlay.yaml` merged over `agent.yaml`, `OverlayError`, framework-key rejection) in PR #956 is REPLACED, not amended.

## What the code actually does

Five facts the design is built on. Each is executed code, not inference.

### The live subscription set is per session, and it is not the workspace list

`bobi/service.py:630-647` composes the manager's topic list: `discover_subscriptions` (`:630`), then `--subscribe` extras (`:631`), then `monitor_subscription_keys(...)` (`:639-641`), then `lifecycle_subscription_keys()` (`:645-647`).
That list reaches the session at `:764`.
`bobi/session.py:1657-1660` then prepends `inbox/<session name>` before registering, and `_start_event_subscription` (`bobi/subagent.py:1715`) registers exactly those keys.

So the live set is `["inbox/<session>"] + composed`, and `{"replace": [...]}` is authoritative: `event-server/core/src/core.ts:1522-1530` removes every stored subscription not in `desired`, and `:1535` assigns `deployment.subscriptions = desired`.

A PUT built from the workspace list alone would unsubscribe the manager from monitor findings, from sub-agent `session.completed`/`session.failed`, and from its own inbox, which is what makes it addressable by `bobi agent <name> message` and `ask`.
A PUT built by recomputing the composition would re-sync whatever the monitor set happens to be at that moment: `bobi/cli.py:3102`, `:3129` and `:3153` write `package/monitors.yaml` at runtime under `with_mutable_runtime_package`, and the manager does not re-PUT on those.

The design therefore never recomposes the set.
It applies the workspace file's delta to the set the server last accepted.

### The deaf-reconnect hook belongs to every session

`_resubscribe_on_deaf` is defined once inside `_start_event_subscription` (`bobi/subagent.py:1974-1982`) and wired into every client at `:1990`.
The only callers of `_start_event_subscription` are `bobi/session.py:1673` and `:1710`, which serve every session: the manager gets `["inbox/<self>"] + composed`, a worker gets `["inbox/<self>"]` only.
It runs on a daemon thread (`bobi/events/client.py:440-446`), entered only after `_HEARTBEAT_TIMEOUT_S = 95.0` seconds of no pong (`:260`, `:294-297`).

Today the hook replays the in-process `active_subscriptions` (`bobi/subagent.py:1765`, reassigned at `:1835` and `:1892`, read at `:1982`), so a CLI change applied to the server would be reverted by the next deaf reconnect.
Sourcing it from the manager's composed list instead would push the manager's `github:`/`slack:`/`linear:` topics onto a worker's own deployment on the worker's next reconnect.
Per-session deployments exist precisely to prevent that: `bobi/events/state.py:21-25` and `tests/integration/test_event_isolation.py:1-16` record the 2026-06-12 incident where project leads received the user's Slack DMs to the director and replied to them.

The hook must stay per session.

### The empty-list fallthrough is a real hazard, and the schema-invalid case is worse

`bobi/events/subscriptions.py:61` gates on `if explicit:`.
An empty list is falsy, so control falls through to `bobi/events/subscriptions.py:68-78`, which calls `Config.load` and then `detect()`, and finally returns `[project_path.name]`.
An operator-emptied list does not mean "subscribe to nothing"; it means "auto-detect from git remotes".

The call at `bobi/events/subscriptions.py:60` also sits inside a `try` whose `except Exception: pass` at `:63-66` is deliberate and test-pinned by `tests/test_ingress.py:306`, so a parse error is swallowed into the same auto-detection.

Measured at `70db2e10`, that fallthrough collapses this team's four GitHub topics to one: `_detect_github` (`bobi/events/adapters.py:57`) returns `['github:moda-labs/bobi-agent']`.
`bobi/events/adapters.py:142` names the Slack hazard in the same path ("silently promote a channel-scoped subscription to a workspace-wide one"), and `_detect_linear` (`:290-315`) subscribes to every Linear team the API key can see rather than MDS and MOD.

Worse, the pack parser maps whole classes of wrong-but-valid YAML to `[]` rather than raising.
A non-dict document root returns `[]` (`bobi/events/subscriptions.py:44-45`), an empty file becomes `{}` through `yaml.safe_load(...) or {}` (`:43`) and then `[]`, and any `subscribe:` value that is neither a string nor a list returns `[]` (`:13-22`).
Under a design where `[]` is authoritative, a truncated file, `{}`, `subscribe: {}`, or a bare list at the document root would silently become "subscribe to nothing".

The pack `subscribe:` is tolerable behind this permissiveness because `<run>/package/` is read-only.
A workspace file is not: `<run>/workspace/` is writable, is edited by the CLI by design, and is advertised to the agent brain as its own scratch space (`bobi/prompts/resolver.py:86-97`).
It needs its own strict validator, and it must fail loud rather than fall through.

### The ingress warning reads a different function, and `doctor` reads it too

`bobi/ingress.py:83` reads `explicit_subscriptions`, not `discover_subscriptions`.
`tests/test_ingress.py:260-276` pins the two to one parser, and the `explicit_subscriptions` docstring at `:25` names itself "The ONE parser for the `subscribe:` surface (D078)".

A workspace layer added only to `discover_subscriptions` would silently break that invariant while the test stayed green, because the test writes no workspace file.
The workspace layer goes inside `explicit_subscriptions`.

That also makes `bobi doctor` a reader, through `_check_ingress_reachability` (`bobi/doctor.py:663`, called at `:51`).

### Authorization is a network write, it fails open, and it covers two services

`authorize_resources` (`bobi/events/server.py:680`) is not a filter.
It POSTs the real GitHub or Linear credential to `/resources/authorize` once per global topic (`:648-655`, called per topic at `:781-784`), needs the signed bubble identity from `<run>/state/bubble.json`, and returns the list UNFILTERED when that identity is absent (`:714`: "can't sign, leave the set unchanged").

It writes grants for two services only: `_RESOURCE_CRED_KEYS` at `bobi/events/server.py:640` is `{"github": ..., "linear": ...}`, and `:761-765` passes `slack:`/`whatsapp:`/`discord:` through untouched because their grants come from the channel registrations in `_register_channel_credentials` (`bobi/subagent.py:1767`), which runs at session start.
The server checks all five prefixes (`core.ts:411`, `:1506-1512`), and the slack grant is keyed on the TEAM id rather than the channel (`core.ts:445-450`), so a new channel inside an already-registered workspace passes while a new workspace does not.

Grants are permanent: `event-server/worker/src/index.ts:147-165` writes them with no `expirationTtl`, and `core.ts` has no revocation route.
`filter_unauthorized=False` is the deliberate policy for an existing deployment, and both the function docstring (`bobi/events/server.py:706-711`) and `_sync_saved_deployment` (`bobi/subagent.py:1859-1866`) give the same reason: a filtered list "would silently unsubscribe a valid existing deployment".
The CLI PUTs to that same deployment, so it uses the same mode.

### The server rejects; it does not silently drop

`core.ts:1509-1511` calls `unauthorizedGlobalTopics` and, on any ungranted topic, returns `400 {"error":"unauthorized_topics","topics":[...]}`, rejecting the whole update before any write.
`core.ts:1499` returns `400 replace[] must not be empty`, so an empty replace is never a legal repair.

The only silent drop is client-side, in `authorize_resources` with `filter_unauthorized=True`, which only the register path uses: that is the parameter default (`bobi/events/server.py:680`) and the call site at `bobi/subagent.py:1805-1808` passes no keyword.
The register path parses that 400 into `UnauthorizedTopics` (`bobi/events/server.py:627`, raised at `:840-842`) and retries once after 0.5s because, after a successful authorize, it "almost always means Cloudflare KV has not yet propagated a just-written grant" (`:856-868`).
The PUT path does neither: `bobi/subagent.py:1926-1946` reaches `resp.raise_for_status()` first and discards the topic list, and it has no retry.

### `managed_repos` has exactly one consumer

`grep -rn "managed_repos" bobi/` exits 1.
The only bobi-agent hits are `tests/test_memory.py:52`, `:55`, `:62`, `:66`, where the string is arbitrary memory-index test data and not the pack key.

Fleet-wide there is one real consumer: `moda-labs/moda-agents` `scripts/check-deploy-compose.py:85-94` parses `managed_repos` off the composed pack `agent.yaml` and fails the deploy-compose check unless `moda-labs/moda-skills` maps to `github-issues`.
The director ROLE prompt is not a consumer today: `agents/moda-eng-team/roles/director/ROLE.md:12` and `:22` say "from configuration" and "in configuration" and never name the key, so `agents/moda-eng-team/agent.yaml:155` ("Advisory tracker binding read by the director prompt") is already false.
D5's prompt half is an addition, not an update.

## Distributed state

This section is the design's spine.
Everything after it is a consequence of it.
Six review rounds each found the same shape of defect in a new place, because the document had no single place naming every source of truth, every writer, and every crash point.

### Sources of truth

| # | State | Location | Owner | Authoritative for |
| --- | --- | --- | --- | --- |
| S1 | pack `subscribe:` | `<run>/package/agent.yaml` | the pack author, read-only at runtime (`bobi/runtime_guard.py:143-153`) | the seed, before any operator edit |
| S2 | declared list | `<run>/workspace/subscriptions.yaml` | the operator; the CLI is the supported writer, but see WX | what this team intends to hear |
| S3 | derived set | computed, never stored | the framework | monitor, lifecycle and inbox routing |
| S4 | deployment credential | `<run>/state/deployments/<session>.json` (`deployment_state_path`, `bobi/events/state.py:32-34`) | the framework, one per session | the identity every PUT targets |
| S5 | accepted record | `<run>/state/subscriptions/<session>.json` | the framework, one per session | what a reconnect should re-assert |
| S6 | routing index, deployment record and eviction clock | the event server | the server | what actually routes, and whether this identity still exists |

S6 is the only state that routes events.
S2 is the only durable record of operator intent.
S5 is a cache with one consumer, and it is never a correctness source for S6.
S4 is in the list because the server can delete the identity it names while the file survives, which is W5 below.

### Invariants

**I1.** S2 never grants and never revokes a derived key.
The CLI refuses to write a currently-derived key into S2, and an S2 removal never removes a key S3 currently owns.
S2 and S3 are NOT guaranteed disjoint, because the overlap can be created from the S3 side: `bobi/cli.py:3097` lets an operator give a new monitor any `event` string, so creating a `monitor/support.email` monitor makes bare `support.email` derived (`bobi/events/subscriptions.py:127-130`) even though it was a legal S2 declaration when it was added, with no subscription command involved.
`effective_monitors` is recomputed per boot (`bobi/monitors/registry.py:120-124`), so membership of S3 is time-varying and an invariant of permanent disjointness is not holdable.
What is holdable is that the derived layer wins: while a key is in S3 it routes regardless of S2, and when it leaves S3 it routes only if S2 still declares it.

**I2.** S5's `subscriptions` is never empty.
A writer with an empty desired set is a bug and refuses rather than recording.
An empty S5 would make every later repair PUT a `{"replace": []}` the server rejects forever (`core.ts:1499`), with the exception swallowed on a daemon thread.

**I3.** S5 holds a set that the server accepted at or before `at`, or (when `applied` is false) a set a writer claimed and whose server outcome is unknown.
S5 is never a claim that S6 currently holds that set.
The codebase states S6 can go stale after acceptance independently of any client: the reconnect comment at `bobi/events/client.py:437-439` and the hook docstring at `bobi/subagent.py:1977-1980` both exist for that case.
D8 settles the consequence: `list` labels S5 "last accepted at T" and makes no live claim.

**I4.** Every write to S6 is a full `replace`, so it is idempotent.
Replaying a set that is already live is a no-op at the server beyond `added: 0, removed: 0`.
This is what makes every recovery path in this design a retry of the same operation rather than a compensating action.

**I5.** The manager's own session start re-asserts S2 and S3 onto S6, PROVIDED its S4 identity still exists on the server.
`bobi/service.py:630-647` recomposes from `discover_subscriptions`, which reads S2 once it exists, and `_sync_saved_deployment` or `_register_with_retry` PUTs the result.
So a start is the reconciliation point for every divergence this design can produce, and the window is one manager start.

The proviso is load-bearing and it is not a corner case.
Both server implementations evict a deployment whose WebSocket has been gone too long.
The local server uses a 60s threshold keyed on WS-disconnect, not on activity (`event-server/src/local.ts:71-78`), and its sweep removes every subscription and then the deployment itself (`event-server/src/local.ts:838-847`).
The Worker does the same from the Durable Object alarm, deleting all three deployment KV keys (`event-server/worker/src/deployment-session.ts:245-286`).
`tests/integration/test_inbox_transport.py:291` already proves the behaviour.

So a manager stopped for more than a minute loses its server-side identity while S4 and S5 survive on disk.
The next non-fresh start takes `_sync_saved_deployment`, its PUT gets 403, and the code DELIBERATELY keeps the stale credential rather than re-registering, because re-registering would supersede the deployment and lose replay continuity (`bobi/subagent.py:1901-1906`: "replay continuity is unverified; check server and credentials before deliberately resetting", and `:1917-1920`: "saved deployment and completion cursor retained; retry pending").

Therefore a persist-only `add` does NOT reliably become live at the next start, and a restart is not an unconditional repair.
Recovering an evicted identity is a decision this spec does not take; see "Open questions".

**I6.** No lock is held across a network round-trip between processes.
`fsutil.file_lock` is a blocking `fcntl.flock(..., LOCK_EX)` with no deadline and no `LOCK_NB` (`bobi/fsutil.py:180`), and no caller anywhere in `bobi/` uses a timed or non-blocking variant.
Holding it across `authorize_resources` (one 10s POST per global topic, `bobi/events/server.py:653` called per topic at `:781-784`) plus a 10s PUT would let a stalled CLI block a manager start with no bound at all.

### Declared versus derived ownership

The two layers overlap as strings, so ownership cannot be expressed as a prefix list.

S3 is composed at three sites, and two of them emit BOTH the source-qualified topic and the bare delivered type:

- `lifecycle_subscription_keys` returns `session.completed` and `session.failed` alongside their `agent/` forms (`bobi/events/subscriptions.py:88-105`, from `LIFECYCLE_EVENTS` at `:85`).
- `monitor_subscription_keys` does the same for every effective monitor event, so a team with a `monitor/support.email` monitor also has bare `support.email` composed (`:108-130`).
- `inbox/<session name>` comes from the session's own identity (`bobi/session.py:1657-1660`).

`docs/EVENT_SERVER.md:374-376` documents why: current servers route a posted event onto both forms, older ones only onto the bare type, so clients subscribe to both.

Two consequences follow, and a four-prefix rejection list gets both of them wrong.

It would be incomplete, because a bare derived key like `session.completed` matches no `agent/`, `monitor/`, `inbox/` or `reply/` prefix.
An operator could add it to S2 and later remove it, which is a real S2 delta, and the authoritative PUT would then drop framework-owned lifecycle routing.

It would be overbroad, because `agent/` is a real event namespace and not a synonym for the two lifecycle keys.
`agent/auto_dispatch.failed` is published by the reactor (`bobi/events/reactor.py:382`), documented as a subscribable topic (`docs/EVENT_SERVER.md:371`, `docs/MONITORS.md:256`), and already used as an `auto_dispatch` rule event (`tests/test_reactor.py:554`).
A rule cannot fire on an event the deployment does not receive, so rejecting the prefix would make a documented topic unsubscribable and silently narrow D1.

**The ownership rule is computed from S3, not matched against prefixes.**
Let `D` be the derived set for the manager session, computed at command time from exactly those three sites.

- `add T` is rejected when `T` is any `inbox/` or `reply/` key.
  These belong to a session identity, never to a declared list.
  This closes `add inbox/<worker session>`, which is the 2026-06-12 cross-session class, and needs no resource grant to exploit (`core.ts:411` makes `inbox/` bubble-scoped, and `core.spec.ts:2251-2262` proves the server stores an arbitrary inbox key).
- `add T` is rejected when `T` is in `D`, with the error naming which site derives it.
  Declaring a key the framework already owns buys nothing and is the only overlap the CLI can create, so refusing it is the cheap half of I1.
  `add agent/auto_dispatch.failed` is therefore accepted, because it is in no composition site.
- `remove T` is rejected when `T` is not in the current S2 list.
  A `remove` that changes nothing in S2 must change nothing on the server, which closes `remove inbox/<manager session>` and `remove session.completed`.
- `remove T` where `T` IS in S2 and ALSO in `D` removes the S2 declaration and does NOT remove the key from the PUT.
  The command says so in its output, naming the monitor or the lifecycle site that still derives it, and exits zero.

That last rule is the half that makes I1 holdable, and it is deliberately not a rejection.
The overlap it handles is created from the S3 side, not by any subscription command: a key declared when nothing derived it becomes derived the moment a matching monitor is configured.
Rejecting the `remove` there would make the declaration permanent, so an operator who later paused that monitor would still be subscribed with no command able to undo it.
Removing the declaration while leaving the derived key routing is the only behaviour that converges: while the monitor exists the key routes because S3 owns it, and once the monitor goes the key stops routing because no S2 declaration remains.

`D` is computed fresh from the same `MonitorRegistry.load(...).effective_monitors()` source the boot composition uses, so the CLI and the next boot cannot disagree about what is derived.
A hand-edited S2 carrying a derived key is harmless at boot regardless, because the composition dedups every append (`bobi/service.py:631` for the `--subscribe` extras, `:640` and `:646` for the derived appends, each gating on membership).

### Writers

Five writers mutate S6 for a given session, and S2 has a sixth writer this design does not control.
Three of the five are in the session process, one is a separate process, and one is the server itself.

| W | Writer | Site | Trigger | Process |
| --- | --- | --- | --- | --- |
| W1 | `_register_with_retry` | `bobi/subagent.py:1811-1835` | session start with no saved deployment | session |
| W2 | `_sync_saved_deployment` | `bobi/subagent.py:1856-1892` | session start with a saved deployment | session |
| W3 | `_resubscribe_on_deaf` | `bobi/subagent.py:1974-1982`, daemon thread per `bobi/events/client.py:440-446` | 95s of no pong, then reconnect | session |
| W4 | `bobi agent <name> subscriptions add\|remove` | new | an operator or the director agent | separate |
| W5 | deployment eviction | `event-server/src/local.ts:838-847`, `event-server/worker/src/deployment-session.ts:245-286` | `EVICTION_STALE_MS` (60s default) of zero WebSocket connections | the event server |
| WX | a hand or agent edit of `workspace/subscriptions.yaml` | none | a human, or the team's own brain, editing its workspace | any |

Rounds 2 through 5 each missed one of these.
Round 2 modelled file writers only, round 4 modelled W1 and W2, round 5 found W3.
W4 is the one this change adds, and W5 and WX are the ones the rewrite's own review found.

**W5 removes, it never adds.** It deletes every subscription and then the deployment (`event-server/src/local.ts:838-847`), so it is not a competing opinion about the topic set but a destruction of the identity the other four write through.
That is why S4 is a source of truth and why I5 carries a proviso.

**WX is a possible writer, not a supported one.** `<run>/workspace/` is writable by design (only `<run>/package/` is guarded, `bobi/runtime_guard.py:143-153`) and is advertised to the agent brain as its own scratch space (`bobi/prompts/resolver.py:86-97`), so neither a human nor the director can be prevented from editing the file directly.
Such an edit takes no lock, so a WX write racing the CLI's read-modify-write at step 5 can be silently overwritten, exactly as `bobi/fsutil.py:169` warns.
The design's answer is not to serialize WX, which is not possible, but to make a bad WX write fail loud through the strict validator and to say in the file's own header comment that the CLI is the supported editor.
The header already says the file is written by the CLI and that comments are not preserved; it gains a line saying a direct edit is not serialized against a concurrent command.

`--subscribe` extras (`bobi/service.py:631`) are NOT a writer in this sense: they enter S6 through W1 or W2 and are never in S2, so the no-S2-delta rule protects them and they drop at the next start without the flag, exactly as today.

### The accepted record

```
<run>/state/subscriptions/<_safe_session(session)>.json
```

```json
{"generation": 7, "subscriptions": ["inbox/eng-team", "github:moda-labs/bobi-agent"],
 "applied": true, "at": "2026-10-09T18:22:04Z", "by": "cli"}
```

A third per-session file beside the deployment record (`bobi/events/state.py:32`) and the event cursor (`:37`), with `_safe_session` (`:28`) applied as both siblings do, because worker session names are caller-chosen (`bobi/subagent.py:836-838`).

It is NOT the deployment record.
`save_deployment_state` (`bobi/events/state.py:55-64`) replaces the whole document on every call, takes no lock and reads nothing first, so a CLI write there could lose a concurrent re-register's new `deployment_id`; the next start would then PUT to a deployment the server deleted, get 403, and boot into a retry loop against the same stale record.
Three tests also assert that document's exact shape (`tests/test_event_subscription.py:71`, `:470-471`, `:514-515`), and the latter two assert it survives a PUT that does not re-mint.

`generation` is a monotonic counter and the concurrency primitive.
`applied` is false between a writer's claim and its confirmation, which is what makes the remote PUT and the local write crash-recoverable without a second file.
`by` is diagnostic only.

### Serialization

**In-process (W1, W2, W3): one module-level `threading.Lock` in `bobi/subagent.py`,** held across each writer's whole claim, apply and confirm sequence, including its network call.
That is safe here and only here: the sole contender is another writer in the same process, every network call it waits behind has a 10s timeout, and no other process can be starved by it.
W3 cannot interleave with a boot writer, which is what round 5's F2 race needed.

**Cross-process (W4 against the session process): `fsutil.file_lock` on the accepted record, held only around a local read and write.**
Microseconds, never across the network, per I6.
`file_lock` gains a `timeout` parameter for this; the default stays blocking so no existing caller changes behaviour.

**No writer mutates the accepted record without holding its lock.**
That rule is absolute, because `file_lock` is advisory and "only serializes writers that also take it" (`bobi/fsutil.py:169`): an unlocked read-then-write can issue a `generation` another writer has already issued, and once two writers hold the same expected generation neither confirmation detects the other.
An unlocked write does not merely risk a lost update, it voids the counter as a compare-and-swap token.

On lock timeout the two sides therefore differ in what they skip, not in whether they take the lock:

- W4 abandons the live apply, keeps the S2 change already written, says another writer is applying, and exits non-zero.
- W1, W2 and W3 proceed with their network apply and SKIP the record entirely, logging at `warning` that reconciliation is pending.
  Their PUT is authoritative at the server regardless, so a manager start is never blocked by a wedged CLI; the record simply keeps its previous contents and the next reconnect or start reconciles it.
  The cost is one window where the record is behind the server, which is the same state the crash table already handles.

**The claim, apply, confirm sequence**, run by W1, W2, W3 and W4:

1. Under the lock: read the record, capture `generation = g`.
   Write `{generation: g+1, subscriptions: <desired>, applied: false, at: now, by: <writer>}`.
   Release.

   W1 and W2 pass a fully composed `<desired>`, because a start is an authoritative re-assert.
   W3 passes what it just read, because it is a replay.
   **W4 passes a DELTA, not a set**, and the claim computes `<desired>` from the record it just read, inside the same lock: `record.subscriptions - removed + added`.
   The workspace file and the accepted record sit behind two different locks, because `file_lock` keys on a companion file per target path (`bobi/fsutil.py:165-167`), so nothing serializes the pair.
   If W4 computed its set outside the record lock, two concurrent `add` calls would each read the same record `A`, compute `A+X` and `A+Y`, and the generation check would pick one and lose the other topic entirely.
   Transforming inside the lock composes them instead: the first claim writes `A+X`, the second reads `A+X` and writes `A+X+Y`.
2. Outside any cross-process lock: authorize if needed, then PUT (or `register`).
3. Under the lock: re-read.
   If `generation != g+1`, another writer superseded this one; leave the record untouched.
   Otherwise write `{generation: g+1, subscriptions: <server echo, else desired>, applied: true, at: now, by: <writer>}`.
   Release.

A reader (W3, or `list`) replays `subscriptions` whether `applied` is true or false.
Replaying an unconfirmed claim is correct by I4: the server either already holds it, in which case the replay is a no-op, or does not, in which case the replay is the repair.

**What this guarantees, and what it does not.**
The claim is written before the PUT, so no writer can revert a change that is already live: a reconnect firing in the window between W4's claim and W4's PUT reads W4's own claimed set.
The generation check means no writer's confirmation can clobber a newer writer's record.
It does NOT guarantee that S5 equals S6 under true concurrency: if two writers' PUTs cross on the wire, the server keeps whichever arrived last while the record keeps whichever confirmed last, and those can differ.
I5 is the repair, within its proviso: a start re-asserts S2 and S3, so the divergence window is bounded by one start of a session whose identity still exists.

The generation counter orders claims; it does not establish freshness, and one case makes that distinction matter.
A boot snapshots its subscription list at `bobi/service.py:630-647` and publishes its pid at `:657`, BEFORE handing the snapshot to the session at `:764`.
`manager_running` is true from that pid alone (`bobi/service.py:846-847`), so between `:657` and W2's claim there is a live-looking manager whose pending PUT carries a PRE-CLI snapshot.
A CLI command in that window would write S2, claim `A+T`, and PUT it, and then W2 would claim `A` with a higher generation and could PUT it last, dropping `T` at the server while the record said `A`.

The CLI's liveness test is therefore not `manager_running` alone.
**L4 requires `manager_running` AND an accepted record stamped at or after the current manager launch**, read from `launch_stamp` (`bobi/launch_stamp.py:76` records it at `bobi/service.py:660`, and `:66` is the stamp path).
Before W2 confirms, the newest record predates this launch, so the command takes the persist-only path and says so.
That converts a silent loss into an honest "nothing applied live, takes effect at the next start", and if the boot's snapshot predated the S2 write the change lands one start later instead, with `list` showing the gap until then.

### Crash points and reconciliation

| Crash point | State left behind | Who reconciles, and how |
| --- | --- | --- |
| During W4's step 4 S2 write | `atomic_write_text` (`bobi/fsutil.py:100`) leaves the old or the new file, never a torn one | nothing to do |
| Between W4's claim and its PUT | S5 claimed, `applied: false`; S6 unchanged | the next W3 or the next start replays or overwrites the claim (I4) |
| Between the PUT and the confirm | S6 holds the new set; S5 says `applied: false` | the next W3 replays the same set (no-op) and confirms it; the next start overwrites S5 with its own authoritative claim |
| During the confirm write (disk full, read-only filesystem) | S6 holds the new set; S5 unconfirmed | same as above; the command reports the local write failure and exits non-zero, having already applied live |
| Inside the server's `replace` loop | S6 partially changed; S5 unconfirmed | see "Accepted failure modes"; re-run the command or restart the manager, both of which are full `replace` retries (I4) |
| Session process killed with the in-process lock held | the lock dies with the process | nothing to do |
| W4 killed holding the file lock | `flock` releases on fd close | nothing to do |
| Record lock timeout on a boot writer | S6 holds the boot's set; S5 keeps its previous contents, possibly with `applied: false` from an earlier claim | the next reconnect or start claims and confirms normally; `list` shows the gap meanwhile |
| Manager down past `EVICTION_STALE_MS` (W5) | S6 identity deleted; S4 and S5 survive on disk | NOT self-healing: the next start's PUT gets 403 and retains the stale credential (`bobi/subagent.py:1901-1906`). See "Open questions" |

The resolution order a reader applies is one rule: **replay `subscriptions` unconditionally.**
There is no second state file, no pending-journal format, and no boot-time resolution pass, because I4 makes an unconditional replay both the confirmation path and the repair path.
If resolution itself fails, the record simply stays unconfirmed and the next reconnect or start repeats it.
`list` surfaces that state so an operator can see it rather than inferring it.

Every row above is self-healing except the last.
Eviction is the one boundary this design cannot reconcile on its own, because the repair means choosing between replay continuity and automatic recovery, which is a decision rather than a mechanism.

## Design

One new workspace file, one strict reader, one new per-session state file, one new CLI group.
No composition function, no change to `bobi/service.py`, no boot-time seeder, no pack template, no new server route, no overlay and no merge semantics.
`UnauthorizedTopics` is reused for the server's rejection; the one new error class is for an invalid workspace file.

### The file

`<run>/workspace/subscriptions.yaml`, a mapping with exactly one key:

```yaml
# Event topics this team subscribes to.
# Created by `bobi agent <name> subscriptions add`, seeded from the agent pack.
# Edit with `bobi agent <name> subscriptions add|remove`, which applies the
# change to the running event server without a restart.
# Comments below this header are not preserved across an edit.
# A direct edit of this file is not serialized against a running command and
# can be overwritten by one; it also applies only at the next manager start.
subscribe:
  - github:moda-labs/lightweave
  - github:moda-labs/moda-agents
  - github:moda-labs/bobi-agent
  - github:moda-labs/moda-skills
  - slack:T0952RZRZ0X:app:A0BDLA833MW:C0BAEN48KQR
  - linear:MDS
  - linear:MOD
```

That body is what this team's first `add` seeds from: `agents/moda-eng-team/agent.yaml:128-134` at `bf9d1088`.

The same `subscribe:` key as the pack, so the wire shape is one shape rather than two.
Monitor topics, lifecycle topics and `inbox/<session>` are not written here, because the CLI refuses to declare a currently-derived key (I1).
A key can still appear in both layers when a later monitor makes it derived, which I1 covers and the ownership rule handles.

The CLI writes the RESOLVED topics, because it seeds from `discover_subscriptions`, which has already interpolated.
The workspace reader does NOT interpolate, so a `${VAR}` in a pack `subscribe:` stops being re-resolved per boot once the file exists.
Only two fleet packs declare `subscribe:` at `bf9d1088` (`agents/moda-eng-team`, `agents/baohua`) and neither uses `${}` inside it, so this is a latent change of contract rather than a live one, stated here rather than discovered later.

The CLI writes a fixed header constant plus `yaml.safe_dump({"subscribe": [...]})`.
Only PyYAML is available (`pyproject.toml:24`), and `safe_dump` discards comments, which the header says out loud rather than working around with line splicing.

### The reader, and its strict validator

`bobi/events/subscriptions.py` gains one function:

```
workspace_subscriptions(project_path) -> list[str] | None
```

`None` means the file is absent.
`[]` means the operator declares nothing, and is returned as such.
Everything else that is not a valid document raises `WorkspaceSubscriptionsError`, a new class in the same module carrying the file path and the reason.

The validator is its own, NOT the permissive pack parser, because under this design `[]` is authoritative and the pack parser maps six distinct wrong inputs to `[]`.
Rejected, each naming the fault:

- an unparseable file (`yaml.YAMLError`)
- an empty file, or a document root that is not a mapping
- a missing `subscribe` key
- a `subscribe` value that is not a list, including the bare string the pack parser accepts
- any element that is not a non-empty string after stripping
- **duplicate topics**, naming the offending topic
- **any unknown top-level key**, naming it

The last two are policy choices and they are stated rather than delegated.
Duplicates are rejected because they make `remove` ambiguous and because the server dedups a `replace` anyway (`core.ts:1518`), so a duplicate can only ever be a hand-edit mistake.
Unknown keys are rejected because this file is machine-written, so `subscriptions:` typed for `subscribe:` must fail closed rather than normalise to "subscribe to nothing", which is the exact hazard the strict validator exists to close.

The pack's historical permissiveness is unchanged.
`explicit_subscriptions` keeps its own parser for `package/agent.yaml`, and `tests/test_symmetric_node.py:90-102` keeps pinning it.

### Where the layer attaches

`explicit_subscriptions` (`bobi/events/subscriptions.py:25`) consults `workspace_subscriptions` first and returns it when it is not `None`, which keeps the D078 one-parser invariant and makes `bobi/ingress.py:83` follow the workspace layer for free.

`discover_subscriptions` (`bobi/events/subscriptions.py:51`) calls `workspace_subscriptions` directly, BEFORE and OUTSIDE the `try` at `:59-66`, and returns the list when it is not `None`.
That placement is the whole point.
Inside the `try`, an invalid workspace file would be swallowed into auto-detection; outside it, the manager boot fails with the path, while an invalid PACK `agent.yaml` keeps its historical fall-through, which `tests/test_ingress.py:306` pins.
That test monkeypatches `explicit_subscriptions`, so `discover_subscriptions` must keep calling it inside the `try` rather than reaching past it to a pack-only reader; otherwise the pin stays green while no longer proving anything.

The pack's own truthiness gate at `bobi/events/subscriptions.py:61` is left alone.
Nothing writes `package/agent.yaml` at runtime, so an empty pack list is not a reachable operator mistake.
The `monitors` commands do write `package/monitors.yaml` under `with_mutable_runtime_package`; `agent.yaml` has no such path.

The file is read twice on an absent-file boot, once by each function.
One window follows: a file created between the two reads is read from inside the `try`, so if it is also invalid the exception is swallowed for that one boot.
The CLI writes with `atomic_write_text`, so this needs a create rather than a torn write, and the next boot fails loud as intended.

The three other readers degrade safely, except `doctor`:

- `bobi/supervisor/snapshot.py:104-109` wraps its call in `except Exception: pass` and is documented best-effort, so silence detection simply asserts less.
- `bobi/service.py:184-195` wraps `check_ingress_reachability` the same way, so the reachability warning is skipped at `log.debug`.
- `bobi/doctor.py` must change: `_check_ingress_reachability` (`:663`) gains an `except WorkspaceSubscriptionsError` arm returning `ok=False` with the path, placed AHEAD of the two arms it already has (`except FileNotFoundError` -> `ok=True, "no agent config"` at `:673-675`, then `except Exception` -> `ok=True, "skipped: ..."` at `:676-678`).
  Both existing arms stay exactly as they are; a missing config must keep reporting `ok=True`.
  Without the new arm an invalid workspace file fails the manager boot while `doctor`, the command an operator runs first, reports green.

### Nothing seeds the file (D6)

The file is created by the first `subscriptions add` or `remove`, and by nothing else.
No boot seeder is needed: with no file the reader returns `None`, `discover_subscriptions` falls through to the pack list exactly as today, and no auto-detection or network call happens.

```
$ cd /tmp && PYTHONPATH=<main worktree> python3 -   # pack with subscribe:, no workspace file,
                                                    # detect() monkeypatched to raise
module file: <main worktree>/bobi/events/subscriptions.py
explicit_subscriptions : ['github:moda-labs/lightweave', 'linear:MDS']
discover_subscriptions : ['github:moda-labs/lightweave', 'linear:MDS']
```

A boot seeder would buy nothing a reader already gets, while costing a `bobi/service.py` change, a pack-non-empty gate (the pack reader cannot tell an absent `subscribe:` from `subscribe: []`, which would make the write an authoritative empty list for the auto-detecting majority of the fleet), and three tests.
Lazy creation is also strictly better on upgrade: until an operator acts, there is no new file and no boot reads differently.

The cost is that `cat workspace/subscriptions.yaml` shows nothing until the first edit, so `list` carries a branch for it.

### The accepted-record API

`bobi/events/state.py` gains, following `session_cursor_path` (`:37`) for the path shape and `atomic_write_json` (`bobi/fsutil.py:143`) for the write:

```
accepted_subscriptions_path(project_path, session) -> Path
load_accepted_subscriptions(project_path, session) -> dict | None
claim_accepted_subscriptions(project_path, session, subscriptions, by) -> tuple[int, list[str]]
claim_accepted_subscriptions_delta(project_path, session, added, removed, by) -> tuple[int, list[str]]
confirm_accepted_subscriptions(project_path, session, generation, subscriptions, by) -> bool
```

`load_` returns `None` for a missing file, a missing or non-list `subscriptions`, or an empty one (I2).
The two `claim_` functions and `confirm_` take the bounded `file_lock` internally and implement steps 1 and 3 of the claim sequence, and each returns the generation it issued plus the set it claimed.
The `_delta` form is W4's, and it computes `record - removed + added` INSIDE the lock, which is what makes two concurrent CLI commands compose rather than one losing its topic.
`confirm_` returns `False` when the generation moved, which is the superseded case.
Neither form ever writes without the lock, per "Serialization"; on timeout they raise `FileLockTimeout` and the caller decides.
`save_deployment_state` (`bobi/events/state.py:55-64`) is not touched, so the CLI never writes the file holding an api key and the three exact-shape assertions on it stay valid.

W1 claims the `authorized` list it is about to pass to `register()` and confirms it on a 201, which proves the server stored that array verbatim (`core.ts:1456-1463`); the register response body carries no `subscriptions` member (`core.ts:1473-1480`), so there is no echo to prefer.
W2 and W4 claim their desired list and confirm with the PUT's echo when the body carries one (`core.ts:1539`) and with the claimed list otherwise, which is why the nine existing tests that answer the PUT with a bare `{"ok": True}` now produce a correct record instead of none.
W3 replays and then confirms the same set.

No `CURRENT_FORMAT_VERSION` bump (`bobi/state_version.py:27`).
The file is new and optional, an older bobi never reads it, and a downgrade degrades to the `active_subscriptions` fallback rather than refusing to start.

### `_resubscribe_on_deaf`

It reads its OWN session's record and PUTs that, falling back to `active_subscriptions` when `load_accepted_subscriptions` returns `None`.
A worker therefore replays `["inbox/<worker>"]`, and the manager replays whatever was last claimed, including a change the CLI made seconds ago.
That fixes the revert without a branch, without recomposition and without any cross-session leak.
It runs inside the in-process lock, which is what excludes it from W1 and W2, and it confirms an unapplied claim on success.

### The CLI

```
bobi agent <name> subscriptions list
bobi agent <name> subscriptions add <topic>...
bobi agent <name> subscriptions remove <topic>...
```

Generic by Zach's own test: the command names a framework concept (a topic), not a domain one (a repo).
It follows the `monitors` group precedent (`bobi/cli.py:3292`).

`<name>` is the INSTALLED slot, not the pack's `agent:` key.
This deployment is installed as `eng-team` while the pack declares `moda-eng-team`, so the director must derive it from `BOBI_ROOT` via `paths.agent_name_for_root` (`bobi/paths.py:92`) rather than guessing.
D1 means the director agent is the primary caller, from inside the runtime; `_detect_project_root` (`bobi/cli.py:80`) honors the inherited `BOBI_ROOT` that the `agent` group binds.

`add` and `remove` each:

1. Resolve the runtime root via `_detect_project_root`, and the manager's session via `manager_session_name(project_path)` (`bobi/service.py:135`).
2. Normalize and validate every topic argument, BEFORE any write and any network call.
   Normalization and validation use the SAME rules as the workspace validator: strip, reject an empty or whitespace-only argument, and reject a duplicate within the argument list.
   The CLI must not be able to write a file its own reader rejects, which would fail the next manager boot: `subscriptions add ''` would otherwise persist an empty string, and the server's update path is no safety net because it validates only that `replace` is a non-empty array (`core.ts:1496-1500`) while the register path checks every element (`core.ts:1399`).
   Then apply the ownership rule.
   A rejected topic fails the whole command non-zero with nothing written, naming the rule and, for a derived topic, the site that derives it.
3. Select the path from the liveness and identity table below, reading `service.team_status(project_path).manager_running` (`bobi/service.py:842-847`), the manager launch stamp (`bobi/launch_stamp.py:66`) and the two state files.
4. Compute the effective list the edit applies to.
   When `workspace/subscriptions.yaml` exists, that is its contents.
   When it is absent, it is `discover_subscriptions(project_path)`, so a team that was auto-detecting keeps its effective set instead of collapsing to the one topic named on the command line.
   This runs under no lock, because on an auto-detecting team it makes live Slack and Linear API calls (`bobi/events/adapters.py:290-315` POSTs to the Linear API with a 5s timeout), and I6 forbids a lock across that.
   The command says in its own output that it made those calls.
5. Under `file_lock` on the workspace file, re-read it, apply the topic edit, and write it back with `atomic_write_text` (`bobi/fsutil.py:100`), per `AGENTS.md:97`.
   The re-read inside the lock is what makes two concurrent `add` calls compose instead of overwriting each other; step 4's value is used only when the file is still absent.
   `add` writes the topic whether or not it was already present, so it is idempotent; `remove` requires the topic to be present, which step 2's ownership rule already enforced.
   Persisting is the whole operation, so a persist-only path stops here, says plainly that nothing was applied live, and exits zero.
   The message says the change takes effect at the next start of a session whose deployment identity the server still holds, and names W5 as the case where that identity is gone, rather than promising a start will apply it.
   Phrasing it that way is the cheap half of Q5: whichever branch Q5 takes, the operator is not told something the code does not guarantee.
6. Claim the accepted record with the DELTA, not with a computed set: `added` is the validated argument list for `add`, and `removed` is the validated argument list for `remove` minus any topic still in `D`.
   `claim_delta` applies it to the record it reads inside the record's own lock, per "Serialization" step 1.
   The delta is the operator's arguments rather than the S2 diff on purpose, for two reasons.
   Two concurrent `add` calls must compose at the server, and an `add` of a topic already in S2 must still be able to apply, which is the D7 recovery path after a grant arrives: computing the delta from the S2 diff would make that re-run a no-op and leave the topic declared but never routed.
   Nothing is recomposed, so monitor, lifecycle, `inbox/` and `--subscribe` topics pass through untouched by construction.
   If the claim is refused because another writer holds the lock past the deadline, stop: the S2 change stands, the command says another writer is applying, and it exits non-zero.
7. For `add` only, call `authorize_resources(..., filter_unauthorized=False)` on the NEWLY ADDED topics alone, so a new `github:` or `linear:` topic has its grant written before the PUT.
   Passing the whole PUT list instead would re-POST the real credential once per already-granted topic (`bobi/events/server.py:781-784` authorizes per global topic), which is seven signed POSTs on this team where one is needed.
   The return value is discarded; `filter_unauthorized=False` is kept so the call can never narrow the set.
   If `load_bubble_state` (`bobi/events/state.py:79`) is empty the command FAILS with that fact, rather than proceeding into the unfiltered pass-through at `bobi/events/server.py:714`.
   `remove` skips this step: the server re-checks every surviving topic (`core.ts:1509`), but grants carry no TTL and have no revocation route, so a survivor granted at an earlier boot still passes.
8. Resolve `es_url` from `cfg.event_server_url` with the `http://localhost:8080` fallback, and `ensure_running` when `local_port_from_url` matches, exactly as `bobi/subagent.py:1948-1955` does.
   Step 3's persist-only paths are what keep a run from ever reaching this line and spawning a server.
9. `PUT /deployments/<id>/subscriptions` via the shared `put_subscriptions` helper, then confirm the record.
   A `403` here means the S4 identity no longer exists on the server, which after a stop longer than `EVICTION_STALE_MS` is the expected answer rather than an anomaly.
   The CLI reports exactly that, names the eviction cause, states that the S2 change is persisted and that a start is needed, and exits non-zero.
   It does NOT re-register: superseding the deployment is the decision held open in "Open questions".
10. Print the full diff of the previous accepted set against the new one, naming every add and every remove, not just the topic the operator typed.
    On `UnauthorizedTopics`, print the topics the server named, state that nothing was applied live, and exit non-zero so a script does not read a rejected apply as success.
    The S2 change written at step 5 is kept, per D7.
    On a superseded confirmation, say the manager applied concurrently and that re-running is safe, and exit non-zero.

### Liveness and identity

Step 3's selection, every row stated:

| # | `manager_running` | deployment record (S4) | accepted record (S5) | S5 stamp vs this launch | Path and exit |
| --- | --- | --- | --- | --- | --- |
| L1 | false | any | any | any | persist-only, exit 0. Nothing is consuming the deployment, so no PUT and no `ensure_running`. |
| L2 | true | absent | any | any | persist-only, exit 0. There is no identity to PUT to. |
| L3 | true | present | absent | n/a | persist-only, exit 0. A pre-upgrade manager is running; it records on its next start, and until then every edit is persist-only. |
| L4 | true | present | present | at or after | live apply, steps 6 to 10. |
| L4b | true | present | present | BEFORE | persist-only, exit 0. The boot published its pid but has not confirmed W2 yet, so its pending PUT carries a pre-command snapshot. |
| L5 | true at step 3, false by step 9 | present | present | at or after | the PUT goes out. It is correct while the identity exists, and a `403` means W5 already evicted it, which step 9 reports and exits non-zero on. |
| L6 | false at step 3, true by step 5 | any | any | any | persist-only was selected, but the boot composition reads S2, so the change applies at that boot if step 5's write landed first and at the following start otherwise. `list` shows the gap. |

L1 is the row file existence gets wrong, which is why it reads the pid.
Nothing unlinks the deployment record on stop: `grep -rn deployment_state_path bobi/` returns only the definition and accessor sites in `bobi/events/state.py`.
PR #1098 added an OS-service stop path that stops the unit and returns without touching event state (`bobi/cli.py:1223-1246`), and its generated units restart the manager unattended (`Restart=on-failure` at `bobi/service_manager.py:120`, `KeepAlive` at `:138`).
`team_status` probes the pid rather than trusting the file (`bobi/service.py:846-847`, `_pid_alive` at `:288`), and normal shutdown unlinks it (`:662-667`), so stopped direct, stopped systemd, stopped launchd and a restart-delay window all answer `False`.

L4b is the fourth column's whole reason for existing, and it is why liveness alone is not the selector.
The boot writes its pid at `bobi/service.py:657` and its launch stamp at `:660`, both AFTER it snapshotted the subscription list at `:630-647` and both BEFORE the session applies it at `:764`.
Comparing the accepted record's `at` against that stamp is what distinguishes "a manager is running and has published its subscription state" from "a manager is starting and has not".

L5 and L6 are windows, not bugs, and neither is closed by re-checking: a re-check has the same race.
L1 is a window of a different kind: it reports honestly, but the change becomes live at the next start only while the identity survives, which after `EVICTION_STALE_MS` it does not.

### `list`

It prints three things:

1. The declared list, labelled as persisted, or as the pack's or auto-detected when no workspace file exists yet, saying in its own output when that resolution made live Slack or Linear calls.
2. The derived group, labelled as derived and not editable: `inbox/<manager session>`, four lifecycle keys (two events, two forms each), and two keys per effective monitor event.
3. The accepted set, labelled **"last accepted at T"** per D8, plus "unconfirmed since T" when `applied` is false.

It flags ONE direction: topics in the declared list that are absent from the accepted set, which is what a dropped or not-yet-applied topic looks like.
The other direction is never flagged, because the derived group and `--subscribe` extras are in the accepted set by design and never in the file, so flagging any difference would fire on every run of a healthy team and bury the real signal.

With no record it says no live set is recorded, naming which of L2 or L3 applies.
It does not PUT, because a read command must not mutate: `core.ts:1522-1536` would make that a write.

Per D8, `list` makes no claim about current routing.
S6 can go stale after acceptance and nothing here detects that; the reconnect re-assert remains the repair, and no GET route is added.

## Exact changes per file

### `moda-labs/bobi-agent`

**`bobi/fsutil.py`**
`file_lock` (`:162`) gains `timeout: float | None = None`.
`None` keeps today's blocking `LOCK_EX` (`:180`) so every existing caller is unchanged; a float retries `LOCK_EX | LOCK_NB` until the deadline and then raises a new `FileLockTimeout`.
This is the only way to satisfy I6, because no timed or non-blocking variant exists anywhere in `bobi/` today.

**`bobi/events/subscriptions.py`**
Add `workspace_subscriptions(project_path) -> list[str] | None` with the strict validator above.
It does not reuse `_normalize_explicit_subscriptions` (`:13`) and it does not interpolate.
Add `WorkspaceSubscriptionsError(path, reason)`, raised by it.
Add `derived_subscription_keys(project_path, session) -> list[str]`, composing `inbox/<session>` plus `monitor_subscription_keys(...)` plus `lifecycle_subscription_keys()` from the same `MonitorRegistry.load(...).effective_monitors()` source `bobi/service.py:636-638` uses, so the ownership rule and the boot composition cannot disagree.
Have `explicit_subscriptions` (`:25`) return the workspace list when it is not `None`.
Have `discover_subscriptions` (`:51`) consult `workspace_subscriptions` before and outside the `try` at `:59-66`, gating on `is not None`.
Leave the pack gate at `:61` unchanged.
No seeder, and no new pack-only reader.

`bobi/service.py` is NOT changed.
`bobi/service.py:630-647` stays exactly as it is.

**`bobi/events/state.py`**
Add `accepted_subscriptions_path`, `load_accepted_subscriptions`, `claim_accepted_subscriptions`, `claim_accepted_subscriptions_delta` and `confirm_accepted_subscriptions`, per "The accepted-record API".
All of them take the bounded `file_lock` internally, around local I/O only, and none of them writes without it.
`save_deployment_state` (`:55-64`) and `deployment_state_path` (`:32-34`) are unchanged.

**`bobi/events/server.py`**
Add a module-level `put_subscriptions(base_url, deployment_id, api_key, subscriptions, *, _retry_unauthorized=True) -> list[str] | None`.
It is the closure at `bobi/subagent.py:1926-1946` moved out, same contract: `bobi/http.py:78` `put` with `timeout=10.0`, body `{"replace": [...], "protocol": protocol_payload()}`, then `raise_for_protocol_error`, then an `UnauthorizedTopics` raise on `400 {"error":"unauthorized_topics"}` matching the register path at `bobi/events/server.py:840-842`, then `raise_for_status`, the `"error" in data` guard, and `validate_server_response`.
The register path raises BEFORE `raise_for_protocol_error` and this one after; either order works, because that function only raises for `invalid_protocol`, `incompatible_protocol` and 426, so it passes a `400 unauthorized_topics` body through untouched.
It carries the SAME bounded single retry `register()` already has (`:856-868`): on `UnauthorizedTopics`, sleep 0.5s and retry once with the flag cleared.
Without it a correct new credential is reported as unauthorized whenever Cloudflare KV has not yet propagated the grant the CLI just wrote, and the in-memory local server cannot reproduce that.
It returns the body's `subscriptions` array, or `None` when the body omits it.
`authorize_resources` (`:680`) is unchanged.

**`bobi/subagent.py`**
Add a module-level `threading.Lock` serializing W1, W2 and W3 per "Serialization".
Delete the `_put_subscriptions` closure at `:1926-1946` and call `events.server.put_subscriptions` instead; it closes over `es_url` and `protocol_payload`, so the CLI cannot reuse it in place.
Wrap `_register_with_retry` (`:1811-1835`) and `_sync_saved_deployment` (`:1856-1892`) in claim, apply, confirm, each inside the in-process lock.
Change `_resubscribe_on_deaf` (`:1974-1982`) to read its own session's record, replay it, and confirm, inside the same lock, falling back to `active_subscriptions` when the record is absent.
On `FileLockTimeout` all three proceed with their network apply and skip the record, logging at `warning` that reconciliation is pending; none of them writes the record unlocked.
Extend `_sync_saved_deployment`'s error classifier (`:1894-1924`) with an `UnauthorizedTopics` branch that names the topics, so the new raise does not downgrade the existing boot message from "resource grants rejected (HTTP 400)" to "unexpected subscription failure".
Keep `filter_unauthorized=False` at `:1883`.
`active_subscriptions` (`:1765`, `:1812`, `:1835`, `:1867`, `:1892`, `:1982`) stays as the fallback.

**`bobi/events/client.py`**
Fix the stale "(and re-registers on failure)" clause at `:501-502`: only `_register_with_retry` re-registers and unlinks the cursor (`bobi/subagent.py:1838`), while `_sync_saved_deployment`'s PUT error path retains both and says so at `:1917-1920` ("saved deployment and completion cursor retained; retry pending").
Raise the swallowed-exception log at `:518-519` from `debug` to `warning`, so a voided repair PUT is visible.

**`bobi/doctor.py`**
`_check_ingress_reachability` (`:663`) gains `except WorkspaceSubscriptionsError` returning `ok=False` with the file path, ahead of the existing `except FileNotFoundError` (`:673-675`) and `except Exception` (`:676-678`) arms, both unchanged.

**`bobi/cli.py`**
Add the `subscriptions` group with `list`, `add`, `remove`, then `main.add_command(subscriptions)`.
Add `"subscriptions"` to the agent-group list at `:4305-4307`, without which `bobi agent <name> subscriptions` does not resolve.
Add it to the pop list at `:4313-4319` so it is not also a top-level command.
Import `bobi.service` lazily for `team_status` and `manager_session_name`, as `:53`, `:577` and `:1232` already do, and `bobi.launch_stamp` for the manager stamp L4 compares against (`bobi/launch_stamp.py:66`, `:37`).

**`tests/test_event_subscription.py`**
The three exact-shape assertions on the deployment record (`:71`, `:470-471`, `:514-515`) stay unchanged, which is the proof that the new write never touches the credential document.
The accepted record now exists after those tests' PUTs, because a claim precedes the PUT and the confirm falls back to the claimed list when the `{"ok": True}` body (`:456`, `:499`) omits an echo.
Those mock replies are NOT changed: the minimal legacy reply is the property they exist to prove.

**Docs**
`docs/EVENT_SERVER.md:378-398`: the resolution order at `:382-394` gains a level above "Explicit", and the "On top of that" paragraph at `:396-398` stays correct.
`docs/BUILDING_AGENT_TEAMS.md:129-133`: `subscribe:` stops being the live source of truth once a workspace file exists, and omitting it still means auto-detection.
`skills/bobi.md`: the new CLI group, which `AGENTS.md:12` names as the CLI command reference.
`docs/EVENT_SERVER.md` also gains the eviction consequence for a persist-only edit, in whichever form Q5 is answered.
`AGENTS.md:133` requires all of these in the same PR, never as a follow-up.

### `moda-labs/moda-agents` (companion PR)

**`agents/moda-eng-team/agent.yaml`**
Remove the `managed_repos:` key and its entire block, from `:151` through EOF at `:162`, as a block rather than a line range so the edit cannot half-apply and orphan `:161-162` into invalid YAML.
Leave `subscribe:` (`:127-134`) in place: it is the seed, and removing it would strand a fresh deployment with nothing to seed from.
Update the `subscribe:` header comment (`:103-126`) to say the list is a seed, not the live source of truth, and drop the now-wrong redeploy sentence at `:124-126`.

**`scripts/check-deploy-compose.py`**
Drop the `managed_repos` assertion at `:85-94`, which cannot pass once the key leaves the pack.
Keep the `subscribe:` assertion at `:76-83`: the seed must still carry moda-skills.

**`agents/moda-eng-team/workspace/managed-repos.yaml`** (new)
The director-maintained tracker bindings, carrying the content removed from `agents/moda-eng-team/agent.yaml:151-162`.
Seeded by the existing `seed_workspace` (`bobi/install.py:174-192`), which copies pack `workspace/` templates only if absent, so a later install never overwrites director edits.
Four packs already ship a `workspace/` directory through that path (`gtm-team`, `market-research`, `support-manager`, `zachs-personal-assistant`), so the mechanism is proven.
`subscriptions.yaml` is deliberately NOT shipped this way, per D6.

**`agents/moda-eng-team/roles/director/ROLE.md`**
Point `:12` and `:22` at `workspace/managed-repos.yaml` instead of the unnamed "configuration", and say the director keeps that file current.
Resolve the agent name from `BOBI_ROOT` rather than a literal `<name>` in any command the prompt prints: this deployment is installed as `eng-team` while the pack's `agent:` is `moda-eng-team`.

**`agents/baohua/`** (comment-only)
`agents/baohua/agent.yaml:105` and `agents/baohua/agent.yaml:118`, and `agents/baohua/README.md:152`, describe the pack `subscribe:` short-circuit as the top of the resolution order.
Update the prose; baohua's behaviour is unchanged, since its pack list is still its seed.

## Accepted failure modes

Two failure modes are accepted rather than fixed.
Both are stated with how an operator sees them and what repairs them, because silence is what turned each of them into a review finding.

### The server's `replace` is not failure-atomic after validation (D9)

The authorization gate rejects the whole update before any write (`core.ts:1506-1512`), so an ungranted topic never half-applies.
After validation passes, `replace` runs independent storage operations in sequence with no transaction and no rollback: the interface at `core.ts:285-292` exposes `putDeployment`, `addSubscription` and `removeSubscription` separately, and the loop at `:1522-1536` calls them one after another.
A storage failure after some removals but before the additions or the `putDeployment` leaves the routing index partially changed while the deployment record and the accepted record keep the old set.

This is pre-existing event-server behaviour that this change neither introduces nor misstates.
What this change does is turn the window into an operator-triggered authoritative mutation, which is why D9 records the acceptance explicitly rather than leaving it unsaid.

**Detection.** The symptom is behavioural: a topic the CLI reported as applied whose events never arrive, or a removed topic that keeps delivering.
There is no API to read the index (D8), so there is no direct check.
The CLI surfaces the only local signal it has, which is that the PUT raised and the record is unconfirmed.

**Recovery.** Both repairs are a full `replace` retry, by I4:

- re-run the same `subscriptions add` or `remove`, which re-applies the delta to the current record and PUTs the result, or
- restart the manager, whose boot PUT re-asserts S2 and S3 (I5).

Either converges while the S4 identity exists, and neither needs a compensating action, because `replace` is idempotent and the desired set is recomputable from S2.
If the partial replace happened because the manager was already gone long enough for W5 to evict it, neither repair reaches the old deployment and the recovery is whatever "Open questions" settles.

**Why acceptance is tenable.** The window needs a storage failure inside a single Durable Object request; every repair path is a retry of the same idempotent operation; and a manager start already re-asserts the full set.

### A rejected `add` leaves the boot PUT failing until it is undone (D7)

D7 keeps the workspace change when the server rejects the apply, so S2 can hold a topic with no resource grant.
The consequence must be stated, because it outlives the command.

The next manager start composes its boot PUT from S2 (`bobi/service.py:630`), so that PUT carries the ungranted topic.
`_sync_saved_deployment` passes `filter_unauthorized=False` (`bobi/subagent.py:1883`), so the topic is kept, and the server 400s the whole update (`core.ts:1509-1511`).
The boot subscription sync therefore fails as a unit: the server keeps the previously accepted set, routing continues on it, and the deployment and cursor are retained (`bobi/subagent.py:1917-1920`).
The classifier reports "resource grants rejected (HTTP 400)" naming the topics, which is why the `UnauthorizedTopics` branch is a required change above.

So the team keeps working on its old routing while every start and every deaf reconnect re-attempts the same rejected PUT.
That is the correct outcome for the normal case, where the grant arrives moments later via the GitHub App install and the next start simply succeeds.

**Recovery** is `subscriptions remove <topic>`, which needs no grant (step 7 skips authorization for `remove`), or fixing the grant upstream.
`list` flags the topic as declared and not accepted, which is exactly the one direction it flags.

## Test plan

The risk lives in the real event server, in cross-session isolation, and in the claim protocol, so the load-bearing tests are integration tests where the harness can reach them and unit tests on the real seam where it cannot.

Every test below is one of two kinds, and each is labelled.
An ACCEPTANCE test must be red against the pre-change code for the right reason; a test that passes before the change proves nothing about the change and does not count as acceptance coverage.
A CHARACTERIZATION test pins existing behaviour this design depends on and is green before the change by definition; it is listed because the behaviour is uncovered today, not because it validates the change.
Every test below is an acceptance test unless it is labelled CHARACTERIZATION.

**Integration, against the real local event server**

The fixture is `tests/integration/test_event_server.py:371` (`event_server`), with `:394` for a deployment and `:62` (`_seed_resource_grants`) for grants.
`tests/integration/test_e2e_event_flow.py:87` already boots a real manager against a real event server and publishes a `github:` topic through to it, and `tests/integration/test_event_isolation.py:211` already runs two real session subprocesses against one server.

1. CHARACTERIZATION. An ungranted global topic in a `replace` returns `400 unauthorized_topics` naming it, and the deployment's prior subscriptions are unchanged.
   This is already implemented (`core.ts:1506-1512`), so it is green before the change; it is here because it is uncovered: `event-server/test/core.spec.ts` asserts `unauthorized_topics` only for the register path (`:2030`), never for `handleUpdateSubscriptions`.
   The whole no-partial-write argument and D7's consequence both rest on it, so it gets a test even though it cannot be an acceptance test.
2. A CLI `add` makes the server route the new topic and a CLI `remove` stops it, with no restart, and the deployment id and api key are unchanged across both, proving identity is preserved and no `register()` happened.
3. After a CLI `add`, the live set still contains every monitor key, both forms of both lifecycle keys, and `inbox/<manager session>`: a published `agent/session.completed` is still delivered, and `bobi agent <name> message` still reaches the manager.
   The same test pauses a monitor between boot and the `add` and asserts the paused monitor's keys are STILL in the live set afterwards, which proves nothing was recomposed.
4. Cross-session isolation survives the change: on the `test_event_isolation.py` harness, a worker's deployment never gains a `github:` topic while the manager's CLI change is applied.
5. Two concurrent `add` calls from a deterministic barrier where BOTH read the same initial accepted record, asserting all four observables: both topics in S2, both in the accepted record, both routing on the live server, and the loser's exit status; then re-run the loser and prove the end state is unchanged.
   Asserting S2 alone would pass in exactly the state the test exists to rule out, because both topics survive S2's lock whether or not either reaches the server.
   The barrier is the point: with the delta applied inside the record lock the two claims compose to `A+X+Y`, and a version that computes the set outside the lock loses one topic and is caught here.
6. Eviction then restart: stop the manager, let `EVICTION_STALE_MS` elapse, and assert that the next start's PUT gets `403`, that the deployment record and cursor are retained (`bobi/subagent.py:1917-1920`), and that a previously persisted `add` is therefore NOT live.
   This is the test that pins I5's proviso rather than its happy path.
   The harness exists: `tests/integration/test_inbox_transport.py:291` already drives eviction with a fast-threshold server fixture.
7. The CLI against a booting manager: with the pid and launch stamp written but W2 not yet confirmed, `subscriptions add` takes the persist-only path (L4b), reports that nothing was applied live, and exits zero, and the topic is live after the boot that follows.
   A version selecting on `manager_running` alone instead loses the topic silently, which is what this test catches.

**Unit, on the claim protocol**

The deaf path is entered only after `_HEARTBEAT_TIMEOUT_S = 95.0` seconds of no pong (`bobi/events/client.py:260`, `:294-297`), so these drive `on_deaf_reconnect` off a patched client exactly as `tests/test_event_subscription.py:110-136` already does, with no 95s wait.

8. A worker session's deaf reconnect PUTs only `inbox/<worker>`, never a `github:`/`slack:`/`linear:` topic, with a MANAGER record also on disk, so it proves the hook reads its own session's record.
   This extends `test_deaf_reconnect_uses_filtered_registered_subscriptions` (`tests/test_event_subscription.py:110-136`), which already asserts the `["inbox/self"]` PUT; the own-record half is the new coverage.
9. A manager's deaf reconnect after a CLI change PUTs the recorded set, not the boot-time list.
10. The claim ordering: a deterministic barrier fires the reconnect hook between the CLI's claim and the CLI's PUT, and the final server set equals the final record.
   A second leg fires it between the PUT and the confirm and asserts the replay is a no-op that flips `applied` to true.
11. A crash between the PUT and the confirm leaves `applied: false` with the new set; the next reconnect replays it and confirms; a manager start instead overwrites it with its own claim.
   Plus a disk-full confirm: the command exits non-zero having applied live, and the record stays unconfirmed rather than wrong.
12. Generation CAS: a confirm whose generation moved returns `False` and leaves the newer record intact; the CLI reports "applied concurrently" and exits non-zero.
13. The bounded lock, in three legs.
    A stalled holder does not block a manager start past the deadline: the boot writer proceeds with its PUT, logs at `warning`, and leaves the record untouched.
    The CLI's own timeout makes it persist-only with a non-zero exit.
    And the leg that matters most: a holder paused specifically between reading `g` and writing `g+1` must not let any other writer issue `g+1` too.
    Drive that interleaving directly and assert the second writer either waits or fails, never writes unlocked, because two writers holding the same expected generation would make both confirmations succeed and void the counter.
    `file_lock(timeout=None)` still blocks, proven by an existing-caller test.
14. The PUT retry and the classifier in one: the first PUT answers `400 unauthorized_topics` and the second succeeds; a second leg leaves both answering 400, raises `UnauthorizedTopics`, has `_sync_saved_deployment`'s classifier report "resource grants rejected" rather than "unexpected subscription failure", and has the deaf path log it at `warning` (`bobi/events/client.py:518-519`).
15. I2 holds: a writer whose desired set is empty refuses to claim, so no record can ever drive a `{"replace": []}` the server rejects (`core.ts:1499`).

**Unit, on the subscription surface**

16. With `workspace/subscriptions.yaml` present and `subscribe: []`, `discover_subscriptions` returns `[]`.
    Against the pre-change gate at `bobi/events/subscriptions.py:61` this is red for the right reason: it ignores the file and returns the pack list.
17. The strict validator, one case each: unparseable, empty file, `{}`, missing `subscribe`, non-mapping root, bare-string value, non-list value, non-string element, duplicate topic, unknown top-level key.
    All raise `WorkspaceSubscriptionsError` naming the fault, and none auto-detects: asserted against the real detector rather than a mock, so the test fails if the read is ever moved inside the `try` at `bobi/events/subscriptions.py:59-66`.
    The same test asserts `bobi/supervisor/snapshot.py:104-109` still returns an empty expectation list rather than raising, and that `bobi doctor`'s ingress check reports `ok=False` with the path.
18. `explicit_subscriptions` and `discover_subscriptions` agree with a workspace file present, extending the parametrized `tests/test_ingress.py:260-276` so the D078 pin covers the new precedence.
    Cases: absent file (reader returns `None`), `subscribe: []` (returns `[]`), and a non-empty list.
19. The ownership rule, one assertion per branch.
    Rejected, each with a non-zero exit, nothing written and no PUT: `remove inbox/<manager>`; `add inbox/<worker>`; `add reply/<anything>`; `add session.completed` as derived; `add support.email` on a team that has that monitor; `remove` of a topic absent from S2; and `add ''` plus `add '   '` plus a duplicated argument, which are step 2's normalization rules.
    Accepted: `add agent/auto_dispatch.failed`, which is in no composition site, with the topic in the PUT; and `add support.email` on a team with no such monitor.
20. The time-varying overlap, as one sequence on a team with no `support.email` monitor: `add support.email` succeeds and routes; configure a monitor whose `event` is `monitor/support.email` (`bobi/cli.py:3097`) so the bare form becomes derived; `remove support.email` then removes the S2 declaration, says the monitor still derives it, exits ZERO, and leaves the key in the PUT; finally pause that monitor and assert the next start's composition no longer carries it.
    This is I1's holdable form, and a version that rejects the `remove` because the key is derived strands the declaration permanently, which this test catches.
21. The liveness table, one case per row: L1 with a stale deployment record present, under stopped-direct, stopped-systemd and stopped-launchd; L2; L3 (a live pre-upgrade manager stays persist-only); L4; L4b (covered by integration test 7); L5, where the identity is gone by the PUT and the command exits non-zero naming eviction; and L6's window.
22. `subscriptions add` on a team with no workspace file seeds it from the effective set, so previously auto-detected topics are still present afterwards, and the file holds the RESOLVED topics.
23. `subscriptions add` with an empty bubble state fails with that reason and does not PUT.

Four assertions are deliberately NOT new tests, each already covered.
`replace` returning the accepted set: `event-server/test/core.spec.ts:2284-2306` (`added: 1`, `removed: 1`, the exact `subscriptions` array at `:2301`) and over real HTTP at `tests/integration/test_event_server.py:510-520`.
`{"replace": []}` returning 400: `core.spec.ts:2314-2324`.
A `remove` whose survivors were granted at an earlier boot: `core.spec.ts:2284-2306` seeds the grant before the replace and the replace passes, and CLI step 7 has `remove` skip authorization by construction.
`bobi/service.py` is not changed at all, so `tests/test_service.py:21-26` needs no attention.

## Migration

No behaviour change on upgrade, for any pack shape, because nothing writes a workspace file until an operator runs `add` or `remove`.

Until then every pack resolves its subscriptions exactly as it does today: a pack list short-circuits, and a pack without one auto-detects.
The first boot after the upgrade composes the same list as the last boot before it, claims it, PUTs it, and confirms the server's `added: 0, removed: 0` echo as the first accepted record.
Row L3 covers the window before that boot, when the CLI can persist but not apply.

The deployment id, api key and event cursor are untouched: nothing in this change calls `register()`, nothing unlinks a cursor, and the credential record is not rewritten.

### Release ordering

The fleet's bobi version is pinned in `moda-labs/moda-agents` `.github/fleet-version` and moved automatically by `version-gate.yml` once `ci-canary` proves a release, so the pin's current value is not worth stating here.

1. Land and release the bobi-agent change; let the canary prove it and the pin PR land.
2. Land the moda-agents companion PR.
3. Roll the fleet.

Step 2 must not precede step 1: the director prompt would otherwise name commands a fleet on the pre-change pin does not have.

## Out of scope

- A GET route for live subscriptions, and any server-side introspection.
  D8 settles it: `list` shows the last-accepted set labelled as such, and the reconnect re-assert remains the repair for a stale index.
- Making a `whatsapp:`, `discord:` or new-workspace `slack:` topic apply LIVE from the CLI.
  Their grants are written by the channel registrations the manager runs at session start (`bobi/subagent.py:1767`), which the CLI does not run, so such a topic persists to the workspace file and applies at the next start.
  An in-workspace `slack:` channel does apply live, because the grant is keyed on the team id (`core.ts:445-450`).
- Making the server's `replace` transactional.
  D9 accepts the window; the detection and recovery behaviour is specified above.
- Hot-reloading anything else from the workspace.
  This change applies exactly one key.

## Open questions

D6 through D9 answered the four this spec previously carried.
The rewrite's own review found one new design fork, and it is a decision rather than a defect, so it is posed rather than taken.

**Q5. How does a team recover a deployment the server has evicted?**

Both server implementations delete a deployment whose WebSocket has been gone longer than `EVICTION_STALE_MS`, 60s by default (`event-server/src/local.ts:71-78`, sweep at `:838-847`; the Worker's DO alarm at `event-server/worker/src/deployment-session.ts:245-286`).
S4 and S5 survive on disk, so the next non-fresh start takes `_sync_saved_deployment`, PUTs to an identity that no longer exists, and gets 403.
Today's code then deliberately keeps the stale credential and the cursor rather than re-registering, because re-registering supersedes the deployment and loses replay continuity: `bobi/subagent.py:1901-1906` says "replay continuity is unverified; check server and credentials before deliberately resetting", and `:1917-1920` says "saved deployment and completion cursor retained; retry pending".

That is a pre-existing property, not something this change introduces.
What this change does is make it matter to an operator: a persist-only `add` is told it "takes effect at the next start", and after an eviction that is not true until the identity is recovered.

The two branches:

- **Automatic recovery.** On a 403 at `_sync_saved_deployment`, discard the stale deployment record and re-register, accepting the loss of that deployment's replay position.
  This makes I5 unconditional, makes L1's promise true, and makes a restart an unconditional D9 recovery.
  It changes existing boot behaviour outside this ticket's surface and weakens a deliberate replay-safety stance.
- **Explicit recovery.** Keep today's behaviour and treat the evicted identity as an operator action (`restart --fresh`, or an equivalent).
  This leaves I5's proviso and L1's caveat as stated above, and the spec's job is then to say so in `docs/EVENT_SERVER.md` and in the CLI's own persist-only message so nobody is surprised.
  Nothing in this ticket changes, which is the smaller option.

My reading is that the second branch is the smaller change and keeps a deliberate stance intact, but the first is what makes the promises in this spec unconditional, and that trade is yours.
Nothing else in this design depends on which way it goes: the claim protocol, the ownership rule, the validator and the liveness table are all unaffected.
