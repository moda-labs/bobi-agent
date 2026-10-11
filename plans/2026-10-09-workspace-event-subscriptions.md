# Workspace-owned event subscriptions

Issue: [#952](https://github.com/moda-labs/bobi-agent/issues/952)
Status: spec, awaiting approval (Gate 1).

Every `bobi/`, `event-server/`, `tests/`, `docs/` and `AGENTS.md` file:line below was read at `f712372deb832e8d0817d606dcb17e849143d616` (`origin/main`, 2026-10-10).
Every `moda-agents` file:line was read at `bf9d1088ad08bafd175232daeddcce3ab2d7e76c` (`origin/main`, 2026-10-08).
One coordinate system, no translation table.

This revision is a rewrite to a different design, not a fold.
The previous eight rounds all reviewed a design in which the CLI applied changes to the event server itself while the manager also wrote on boot and on reconnect.
A premise check against main found that every blocker those rounds produced lived in that one choice, and that a single-writer design meets D1 through D9 including live editing with no restart.
The generation counter, claim-then-confirm, the accepted-record file, the launch-stamp liveness table, the bounded file lock, the five-writer model and the computed key-ownership rule are all deleted, because with one writer there is nothing for them to coordinate.
One codex review of this rewrite found three blockers, five majors and two minors, all verified against real code and all folded into the text below rather than appended.

## Problem

Changing which repos a running team hears about means editing agent pack source in a second repository, rebuilding the pack, reinstalling it, and restarting the manager.

The topics live in `subscribe:` in `moda-labs/moda-agents` at `agents/moda-eng-team/agent.yaml:127-134`, installed as a frozen read-only image at `<run>/package/agent.yaml` (`bobi/runtime_guard.py:143-153`).
The 2026-08-03 familystories-ai offboard is the measured case: a commit in another repo, a pack rebuild, an install, and a restart.
The pack comment at `agents/moda-eng-team/agent.yaml:124-126` says so itself: "the server-side subscription set is replaced by `register` on the next manager-session start, so this team must be redeployed for the cut to take effect."

A team cannot change its own subscriptions, so onboarding is gated on a human doing a release in a different repository.

## Decisions

All settled by Zach in Slack `C0BAEN48KQR`, not reopened here.
D1 to D5 are from thread `1791519568.830069` (2026-10-09); D6 to D9 from thread `1791568791.200569` (2026-10-09); D10 to D13 from the same thread on 2026-10-10.

**D1.** The core question is generic: a running team may mutate its own event subscriptions.

**D2.** Subscriptions live as a list in the WORKSPACE; the pack `agent.yaml` `subscribe:` is only the SEED.

**D3.** A generic framework CLI subcommand applies changes, e.g. `bobi agent <name> subscriptions list|add|remove` (topics, not repos: no repo concepts in `bobi/`, and no `repos` CLI, per his August rulings).
It persists the workspace list and hot-applies over the existing identity-preserving `PUT /deployments/<id>/subscriptions {"replace":[...]}`.
No restart needed.

**D4.** Read-back of what is live, as relaxed by D8: `subscriptions list` shows the last-accepted list, labelled "last accepted at T".
Not via the admin channel.

**D5.** `managed_repos` is eng-team specific: remove it from the pack `agent.yaml`, move it into a director-maintained workspace file, and update the eng-team director ROLE prompt to read and keep it current.
Prompt and pack change only, no bobi-agent code.
Accepted trade-off: the director can grant itself branch-delete authority (Zach, August: branches are recoverable).

**D6.** `workspace/subscriptions.yaml` is created lazily, by the CLI, on the first `add` or `remove`.
Nothing seeds it at boot and no pack template ships it.

**D7.** When the server rejects the live apply, the CLI keeps the workspace change, reports the server's exact `unauthorized_topics` list, and exits non-zero.

**D8.** No new GET endpoint on the EVENT SERVER, and server-side introspection stays out of scope.
The existing reconnect re-assert remains the repair for a stale server-side index.

**D9.** The event server's non-atomic `replace` is accepted as a known failure mode.
This spec records the acceptance and states how an operator detects and recovers from a partial replace.

**D10.** An evicted deployment is recovered by an explicit operator action, as today.
Nothing in this ticket changes the boot path's deliberate choice to keep a stale credential rather than re-register.

**D11.** `list` need not work while the manager is down.
No manager-written state file is added, so `list` reads the accepted set out of the running manager's memory and says so when there is none.

**D12.** A reload also hot-applies monitor drift.
A monitor paused since boot stops routing at the next reload, which is convergence toward what the next restart would do anyway.

**D13.** Airtightness is balanced against code complexity.
An edge case that would need non-trivial code to cover is NOT covered; it is listed under "Accepted risks" with its likelihood, impact and the cost of covering it.

The old generic-overlay design (`overlay.yaml` merged over `agent.yaml`, `OverlayError`, framework-key rejection) in PR #956 is REPLACED, not amended.

## What the code actually does

Six facts the design is built on. Each is executed code, not inference.

### The live subscription set is composed per session, and the manager snapshots it before the session exists

`bobi/service.py:630-647` composes the manager's topic list: `discover_subscriptions` (`:630`), then `--subscribe` extras (`:631`), then `monitor_subscription_keys(...)` (`:636-641`), then `lifecycle_subscription_keys()` (`:645-647`).
That list reaches `run_persistent_agent` at `:758` as `subscribe=subscribe` (`:764`), becomes `Session(subscribe=...)` at `bobi/subagent.py:910`, and `bobi/session.py:1657-1660` prepends `inbox/<session name>` before `_start_event_subscription` (`bobi/subagent.py:1715`) registers exactly those keys.

The composition therefore happens at `:630-647`, the manager publishes its pid at `:657`, and the session that applies the list does not exist until `:758`.
Anything that changes the file in that window is overwritten by the snapshot.
A worker gets `["inbox/<self>"]` only, from the same `bobi/session.py:1657-1660` path.

`{"replace": [...]}` is authoritative: `event-server/core/src/core.ts:1522-1530` removes every stored subscription not in `desired`, and `:1535` assigns `deployment.subscriptions = desired`.
So a PUT built from the workspace list alone would unsubscribe the manager from monitor findings, from sub-agent `session.completed`/`session.failed`, and from its own inbox, which is what makes it addressable by `bobi agent <name> message` and `ask`.

### The deaf-reconnect hook replays a cached list

`_resubscribe_on_deaf` is defined once inside `_start_event_subscription` (`bobi/subagent.py:1974-1982`) and wired into every client at `:1990`.
It runs on a daemon thread (`bobi/events/client.py:442-446`), entered only after `_HEARTBEAT_TIMEOUT_S = 95.0` seconds of no pong (`:260`, `:294`).

Today it PUTs the in-process `active_subscriptions` (`bobi/subagent.py:1765`, reassigned at `:1835` and `:1892`, read at `:1982`).
That cache is the only reader of the variable, and it is why a change the server already accepted can be reverted: the server commits `replace` and then responds (`core.ts:1535-1540`), so a lost response leaves `_sync_saved_deployment` short of `active_subscriptions = list(authorized)` (`:1892`) and the next reconnect replays the old list.

Sourcing the hook from the MANAGER's composed list would push the manager's `github:`/`slack:`/`linear:` topics onto a worker's own deployment.
Per-session deployments exist precisely to prevent that: `bobi/events/state.py:21-25` and `tests/integration/test_event_isolation.py:211` record the 2026-06-12 incident where project leads received the user's Slack DMs to the director and replied to them.
Composing per session is what makes recomposition safe, because a worker's own composition is `["inbox/<worker>"]` by construction.

### The empty-list fallthrough is a real hazard, and the schema-invalid case is worse

`bobi/events/subscriptions.py:61` gates on `if explicit:`.
An empty list is falsy, so control falls through to `:68-78`, which calls `Config.load` and then `detect()`, and finally returns `[project_path.name]`.
An operator-emptied list does not mean "subscribe to nothing"; it means "auto-detect from git remotes".

The call at `:60` also sits inside a `try` whose `except Exception: pass` at `:63-66` is deliberate and test-pinned by `tests/test_ingress.py:306-324`, so a parse error is swallowed into the same auto-detection.
The shared parser itself raises (`tests/test_ingress.py:290-303`); it is the call site that swallows.

Measured at `f712372d`, that fallthrough collapses this team's four GitHub topics to one: `_detect_github` (`bobi/events/adapters.py:57`) returns `['github:moda-labs/bobi-agent']`.
`bobi/events/adapters.py:142` names the Slack hazard in the same path ("silently promote a channel-scoped subscription to a workspace-wide one"), and `_detect_linear` (`:290-315`) subscribes to every Linear team the API key can see rather than MDS and MOD.

Worse, the pack parser maps whole classes of wrong-but-valid YAML to `[]` rather than raising: a non-dict document root (`:44-45`), an empty file through `yaml.safe_load(...) or {}` (`:43`), and any `subscribe:` value that is neither a string nor a list (`:13-22`).
Under a design where `[]` is authoritative, a truncated file, `{}`, `subscribe: {}`, or a bare list at the document root would silently become "subscribe to nothing".

The pack `subscribe:` is tolerable behind this permissiveness because `<run>/package/` is read-only.
A workspace file is not: `<run>/workspace/` is writable, is edited by the CLI by design, and is advertised to the agent brain as its own scratch space (`bobi/prompts/resolver.py:86-95`).
It needs its own strict validator, and it must fail loud rather than fall through.

### The ingress warning reads a different function, and `doctor` reads it too

`bobi/ingress.py:83` reads `explicit_subscriptions`, not `discover_subscriptions`.
`tests/test_ingress.py:260-276` pins the two to one parser, and the `explicit_subscriptions` docstring at `bobi/events/subscriptions.py:28` names itself "The ONE parser for the `subscribe:` surface (D078)".

A workspace layer added only to `discover_subscriptions` would silently break that invariant while the test stayed green, because the test writes no workspace file.
The workspace layer goes inside `explicit_subscriptions`.
That also makes `bobi doctor` a reader, through `_check_ingress_reachability` (`bobi/doctor.py:663`, called at `:51`).

### Authorization is a network write, it fails open, and it covers two services

`authorize_resources` (`bobi/events/server.py:680`) is not a filter.
It POSTs the real GitHub or Linear credential to `/resources/authorize` once per global topic (`:643-651`, called per topic at `:781-784`), needs the signed bubble identity from `<run>/state/bubble.json`, and returns the list UNFILTERED when that identity is absent (`:714`: "can't sign, leave the set unchanged").

It writes grants for two services only: `_RESOURCE_CRED_KEYS` at `:640` is `{"github": ..., "linear": ...}`, and `:761-765` passes `slack:`/`whatsapp:`/`discord:` through untouched because their grants come from the channel registrations in `_register_channel_credentials` (`bobi/subagent.py:1767`), which runs at session start.
The server checks all five prefixes (`core.ts:411`, `:1509`), and the slack grant is keyed on the TEAM id rather than the channel, so a new channel inside an already-registered workspace passes while a new workspace does not.

Grants are permanent: `event-server/worker/src/index.ts:147-159` writes them with no `expirationTtl`, and `core.ts` has no revocation route.
`filter_unauthorized=False` is the deliberate policy for an existing deployment, and both the function docstring (`bobi/events/server.py:706-711`) and `_sync_saved_deployment` (`bobi/subagent.py:1859-1866`) give the same reason: a filtered list "would silently unsubscribe a valid existing deployment".
Every PUT in this design goes to that same deployment, so it keeps that mode.

### The server rejects; it does not silently drop. And a reload channel already exists

`core.ts:1509-1512` calls `unauthorizedGlobalTopics` and, on any ungranted topic, returns `400 {"error":"unauthorized_topics","topics":[...]}`, rejecting the whole update before any write.
`core.ts:1499` returns `400 replace[] must not be empty`, so an empty replace is never a legal repair.
The register path parses that 400 into `UnauthorizedTopics` (`bobi/events/server.py:627`, raised at `:841`) and retries once after 0.5s because, after a successful authorize, it "almost always means Cloudflare KV has not yet propagated a just-written grant" (`:856-866`).
The PUT path does neither: `bobi/subagent.py:1926-1946` reaches `resp.raise_for_status()` at `:1943` first and discards the topic list.

For telling a RUNNING manager to re-apply, four channels were checked on main and exactly one works.

- A signal. Absent. `git grep 'SIGHUP\|SIGUSR1\|SIGUSR2' -- bobi/ event-server/ docs/` finds nothing; the only handlers are SIGTERM (`bobi/service.py:683`).
- An event-server admin route. Absent, and D8 rules it out.
- An inbox control message. The right shape, and it DEADLOCKS for the primary caller. The `compact` precedent sends a sentinel framework-side before the model (`bobi/cli.py:1408`, handled at `bobi/session.py:1260`) with `wait=False`, and `wait=False` cannot return `unauthorized_topics`. D1 makes the director agent the primary caller, running the CLI from inside its own turn: the inbox consume loop is serial, `_drain_turn` (`bobi/session.py:888`) holds the session until its terminal result, and that turn cannot finish because its own Bash call is blocked on the CLI.
- The manager's local HTTP server. Works. `bobi/manager_health.py:216` `start()` is called by the manager at `bobi/service.py:687`; it serves on a `ThreadingHTTPServer` with daemon request threads (`:36-51`) from its own daemon thread (`:253-255`), so an in-flight LLM turn does not block it (the session runs in a separate thread, `bobi/session.py:1773`). Its port is published at `state/manager-health.port` (`:250-251`) and unlinked on graceful stop (`:271-273`); `bobi/supervisor/supervision.py:303` and `bobi/webapp/health.py:69` already read it. Routes today are GET-only (`do_GET` at `:75-83`, no `do_POST`), and the handler already reaches into live process state the same way this design needs, with a request-time `from bobi.inbox import get_local_inbox` at `:167`.

## Design

One writer per deployment.

Each session already owns exactly one event-server deployment and is the only process that PUTs it: at boot (`bobi/subagent.py:1957-1960`) and on deaf reconnect (`:1974-1982`).
This change keeps that and adds no second writer.
The CLI only edits `<run>/workspace/subscriptions.yaml`; it never PUTs.
After persisting, it POSTs a token-protected `/subscriptions/reload` on the manager's health server, and the MANAGER's own session re-reads the file, re-composes, applies, and returns the event server's accept or reject in the response body.
So the manager's deployment has three writing call sites of one function in one process (boot, reload, deaf reconnect), and a worker's deployment has the two it has today.

That is what satisfies D1 with no restart, and it is also what deletes every cross-process coordination mechanism the previous design carried.
No generation counter, no claim-then-confirm, no accepted-record file, no launch stamps, no lock timeout, because no two processes write the same set.

### The file

`<run>/workspace/subscriptions.yaml`, a mapping with exactly one key:

```yaml
# Event topics this team subscribes to.
# Created by `bobi agent <name> subscriptions add`, seeded from the agent pack.
# Edit with `bobi agent <name> subscriptions add|remove`, which applies the
# change to the running event server without a restart.
# Comments below this header are not preserved across an edit.
# A direct edit of this file is not serialized against a running command and
# can be overwritten by one; it also applies only at the next reload or start.
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

Monitor topics, lifecycle topics and `inbox/<session>` are never written here; they are re-derived by the composition on every apply.
The CLI writes a fixed header constant plus `yaml.safe_dump({"subscribe": [...]})`.
Only PyYAML is available (`pyproject.toml:24`) and `safe_dump` discards comments, which the header says out loud rather than working around with line splicing.

The CLI writes RESOLVED topics, because it seeds from `discover_subscriptions`, which has already interpolated.
The workspace reader does NOT interpolate, so a `${VAR}` in a pack `subscribe:` stops being re-resolved per boot once the file exists.
That is risk R5 below, not a covered case.

### The reader, and its strict validator

`bobi/events/subscriptions.py` gains:

```
workspace_subscriptions(project_path) -> list[str] | None
```

`None` means the file is absent.
`[]` means the operator declares nothing, and is returned as such.
Everything else that is not a valid document raises `WorkspaceSubscriptionsError`, a new class in the same module carrying the file path and the reason.

The validator is its own, NOT the permissive pack parser, because under this design `[]` is authoritative and the pack parser maps six distinct wrong inputs to `[]`.
Rejected, each naming the fault: an unparseable file (`yaml.YAMLError`); an empty file, or a document root that is not a mapping; a missing `subscribe` key; a `subscribe` value that is not a list, including the bare string the pack parser accepts; any element that is not a non-empty string after stripping; a duplicate topic, naming it; any unknown top-level key, naming it; and **any `inbox/` or `reply/` topic**, naming it.

The last three are policy choices.
Duplicates are rejected because they make `remove` ambiguous and because the server dedups a `replace` anyway (`core.ts:1518`), so a duplicate can only ever be a hand-edit mistake.
Unknown keys are rejected because this file is machine-written, so `subscriptions:` typed for `subscribe:` must fail closed rather than normalise to "subscribe to nothing", which is the exact hazard the strict validator exists to close.

The namespace ban lives HERE, in the reader, and not only in the CLI's argument check.
That placement is load-bearing: the file is writable by a human or by the team's own brain (risk R2), the server accepts any non-empty string at registration (`core.ts:1394-1402`) and treats `inbox/` as an ordinary bubble-scoped topic (`core.ts:406-414`), so a hand-written `inbox/<worker>` would be composed and PUT on the next reload and would route that worker's private messages to the manager.
That is the 2026-06-12 incident class and it needs no resource grant to reach.
Enforcing it in the reader makes every path fail closed: the CLI rejects the argument, a hand edit fails the boot compose with the path, `doctor` reports `ok=False`, and a deaf reconnect raises inside the hook, which `bobi/events/client.py:507-519` catches and logs at `warning` so the socket still heals while no PUT goes out.
Two lines in the validator, reused by the CLI, in place of the previous design's 43-line computed ownership rule.

The pack's historical permissiveness is unchanged; `explicit_subscriptions` keeps its own parser for `package/agent.yaml`.

### Where the layer attaches

`explicit_subscriptions` (`bobi/events/subscriptions.py:25`) consults `workspace_subscriptions` first and returns it when it is not `None`, which keeps the D078 one-parser invariant and makes `bobi/ingress.py:83` follow the workspace layer for free.

`discover_subscriptions` (`:51`) calls `workspace_subscriptions` directly, BEFORE and OUTSIDE the `try` at `:59-66`, and returns the list when it is not `None`.
That placement is the whole point.
Inside the `try`, an invalid workspace file would be swallowed into auto-detection; outside it, the apply fails with the path, while an invalid PACK `agent.yaml` keeps its historical fall-through, which `tests/test_ingress.py:306-324` pins.
That test monkeypatches `explicit_subscriptions` at `:322`, so `discover_subscriptions` must keep calling it inside the `try` rather than reaching past it to a pack-only reader; otherwise the pin stays green while no longer proving anything.

The pack's own truthiness gate at `:61` is left alone.
Nothing writes `package/agent.yaml` at runtime, so an empty pack list is not a reachable operator mistake.

The three other readers degrade safely, except `doctor`:

- `bobi/supervisor/snapshot.py:105-108` wraps its call in `except Exception: pass` and is documented best-effort, so silence detection simply asserts less.
- `bobi/service.py:184-195` wraps `check_ingress_reachability` the same way, so the reachability warning is skipped at `log.debug`.
- `bobi/doctor.py` must change: `_check_ingress_reachability` (`:663`) gains an `except WorkspaceSubscriptionsError` arm returning `ok=False` with the path, placed AHEAD of the two arms it already has (`except FileNotFoundError` -> `ok=True, "no agent config"` at `:675`, then `except Exception` -> `ok=True, "skipped: ..."` at `:678`).
  Both existing arms stay exactly as they are; a missing config must keep reporting `ok=True`.
  Without the new arm an invalid workspace file fails every apply while `doctor`, the command an operator runs first, reports green.

### Nothing seeds the file (D6)

The file is created by the first `subscriptions add` or `remove`, and by nothing else.
With no file the reader returns `None`, `discover_subscriptions` falls through to the pack list exactly as today, and no auto-detection or network call happens.
A boot seeder would buy nothing a reader already gets, while costing a pack-non-empty gate (the pack reader cannot tell an absent `subscribe:` from `subscribe: []`, which would make the write an authoritative empty list for the auto-detecting majority of the fleet).
Lazy creation is also strictly better on upgrade: until an operator acts, there is no new file and no boot reads differently.

### One composition, inside the session

`bobi/events/subscriptions.py` gains the composition itself, moved out of `bobi/service.py:630-647`:

```
compose_session_subscriptions(project_path, session, extra=()) -> list[str]
```

It returns `["inbox/<session>"] + extra` for EVERY session, and then, only when `session == manager_session_name(project_path)` (`bobi/service.py:135`, the same function the manager's own `session_name` comes from at `:713`), appends `discover_subscriptions(project_path)`, `monitor_subscription_keys(...)` from `MonitorRegistry.load(...).effective_monitors()`, and `lifecycle_subscription_keys()`, deduping on append exactly as `:631`, `:640` and `:646` do today.

`extra` is every session's, not the manager's.
`bobi/session.py:339-341` already defines it that way ("Extra event topics beyond this session's own inbox/<self>"), and `bobi agent <name> spawn --subscribe` (`bobi/cli.py:3475-3476`, documented at `bobi/subagent.py:840-841`) is a supported way to launch a persistent worker on an external topic.
Only the workspace, monitor and lifecycle layers are manager-only.
A worker therefore stays `["inbox/<worker>"] + its own extras` with no `discover_subscriptions` call and no network, because the manager-name compare fails first, which is what keeps recomposition from leaking the manager's topics onto it.

Moving the composition into the session narrows the boot-overwrite window from seconds to one function body, but it does not close it: the compose at the top of `_start_event_subscription` still precedes the boot's own register or PUT at `bobi/subagent.py:1957-1960`.
The window is closed by registration order and lock scope, in the next section, not by the move alone.

One composition site, by the house single-path rule:

- `bobi/service.py:630-647` is deleted; `:764` passes the raw `--subscribe` extras instead of a composed list.
- `bobi/session.py:1657-1660` stops prepending `inbox/<self>` and passes `self._subscribe` straight through.
- `_start_event_subscription(session_name, extra_subscribe, project_path, ...)` (`bobi/subagent.py:1715`) calls the composer at the top and uses the result wherever it uses `subscribe` today, including `has_external` at `:1760`.
  Its second parameter changes meaning from "the full key list" to "extra topics"; both call sites (`bobi/session.py:1673`, `:1710`) are in this change.

### Reload, and the one lock

`_start_event_subscription` gains one closure, held on a small controller object:

```
reload(*, authorize: bool) -> dict
```

Under a module-level `threading.Lock` in `bobi/subagent.py` it re-composes from the files, optionally authorizes, PUTs `{"replace": desired}`, records the accepted set, and returns what the server said.
The lock covers COMPOSE AND APPLY TOGETHER, at all four call sites in the session process: the boot register (`_register_with_retry` at `:1811`), the boot sync (`_sync_saved_deployment` at `:1856`), the health route, and `_resubscribe_on_deaf` (`:1974`).
Covering both together is what makes the sequence linearizable: no writer can compose a set, be overtaken by a newer writer, and then land its stale set.
No file lock is ever held across it.

**The boot window is closed by ordering.**
The controller is registered at the TOP of `_start_event_subscription`, before the first compose and before the boot's register or PUT at `:1957-1960`, with its deployment identity initially unset.
A reload POST arriving in that window finds the controller, calls `reload`, and BLOCKS on the module lock until the boot's compose-and-apply completes; it then composes again, reads the file the CLI just wrote, and PUTs the newer set.
So the stale-snapshot class is gone rather than detected, and it needs no pending-reload bookkeeping.
`409 no_live_subscription` is left for the honest case only: no controller is registered at all, because `_start_event_subscription` has not been entered. The boot's own compose then reads the file, so the change still applies at that boot.

`authorize=True` is the operator-triggered path: it runs `authorize_resources(..., filter_unauthorized=False)` over the whole recomposed set, which is exactly what boot already does at `:1881-1883`, so a newly declared `github:` or `linear:` topic has its grant written before the PUT.
The cost is one signed POST per global topic per reload, seven on this team, on an operator command rather than in a loop.
Paying it is deliberate: a delta optimizer would need to know which topics are new, which is the per-caller state single-writer exists to remove.

`authorize=False` is the deaf-reconnect path, which matches today's behaviour (the hook does not authorize) and keeps a transport-recovery path from firing seven credential POSTs.
If the file holds an ungranted topic the PUT 400s there, which is the already-accepted D7 consequence, logged at `warning`.

`_resubscribe_on_deaf` calls `reload(authorize=False)` instead of replaying `active_subscriptions`.
Because the desired set is re-derived from the file rather than from a cache, a PUT whose response was lost is not reverted: the next assert recomputes the same desired set.
`active_subscriptions` (`:1765`, `:1812`, `:1835`, `:1867`, `:1892`, `:1982`) is then write-only and is deleted, which removes the stale-cache class rather than guarding it.

**The accepted record, in memory.**
The controller holds the last accepted set and the time it was accepted, written under the same lock after a successful register or PUT.
It is needed for two things D4 and D11 require and nothing else provides: `GET /subscriptions` has no other source once `active_subscriptions` is gone, and the command's output cannot name which topics changed, because the server returns only numeric `added` and `removed` counters (`core.ts:1537-1540`) and `_put_subscriptions` returns nothing today (`bobi/subagent.py:1926-1946`, which gains a return of the body's `subscriptions` array, or the PUT list when the body omits it, as the nine existing stubs answering a bare `{"ok": True}` do, at `tests/test_event_subscription.py:94`, `:119`, `:214`, `:348`, `:416`, `:456`, `:499`, `:534` and `:562`).

This is NOT the accepted-record protocol the previous design carried.
It is one tuple in the owning process's memory, with no file, no generation counter, no claim and no cross-process reader, so there is nothing to serialize and nothing to reconcile after a crash: a restarted manager simply composes and applies, and its first apply seeds the record.
It is never replayed as the desired set. That distinction is the whole point of deleting `active_subscriptions`, so it is stated rather than left to a future reader.

**One guard on the reconnect path.**
Auto-detection swallows a transient failure into `[]` (`bobi/events/adapters.py:201-205` for Slack, `:313-315` for Linear) and discovery then falls through to `[project_path.name]` (`bobi/events/subscriptions.py:78`).
On a team with NO workspace file, that would let a reconnect PUT a narrower set than the one already accepted and silently stop routing until the next successful apply.
So `reload(authorize=False)` refuses to PUT when there is no workspace file AND the recomposed set is a strict subset of the last accepted set; it logs at `warning` and leaves routing alone.
Three lines, using the record above. With a workspace file a shrink is a legitimate operator removal and is applied.

`_put_subscriptions` (`:1926-1946`) raises the EXISTING `UnauthorizedTopics` (`bobi/events/server.py:627`, carrying `.topics`) on `400 {"error":"unauthorized_topics"}`, before `raise_for_status()` at `:1943`, matching the register path at `bobi/events/server.py:841`, and returns the accepted list instead of `None`.
`_sync_saved_deployment`'s error classifier (`:1894-1924`) gains an `UnauthorizedTopics` branch so the new raise does not downgrade the existing boot message at `:1910` ("subscription configuration or resource grants rejected (HTTP 400)") to the `:1916` catch-all.
The closure stays where it is; the CLI never PUTs, so nothing is extracted to a module.

### The in-process live-subscription registry

The health handler needs a handle on the manager session's live subscription.
It has none today: `Session._subscription` is private (`bobi/session.py:356`, assigned at `:1678` and `:1726`) and the manager `Session` is a local in `run_persistent_agent` (`bobi/subagent.py:894`).

`bobi/subagent.py` gains the registry pattern already in the codebase at `bobi/inbox.py:76-92`: a module dict under a lock, with `register_live_subscription(session, controller)`, `unregister_live_subscription(session)` and `get_live_subscription(session)`.

The controller is a small object, not the `Subscription` dataclass (`bobi/subagent.py:1648-1660`), because it must exist BEFORE the deployment identity does.
`_start_event_subscription` creates and registers it as its first act, fills in its identity under the lock at `:1957-1960`, and holds the `reload` closure and the in-memory accepted record on it.
`Subscription.stop` (`:1662`) unregisters, and a boot that raises before registering an identity unregisters in a `finally`, so a dead controller never answers a reload.
About twenty lines.

### The reload route

`bobi/manager_health.py` gains a `do_POST` and one GET path, and a token.

- Token: `state/manager-health.token`, minted and 0600-locked by the existing `write_secret` (`bobi/webui_common/launcher.py:63-76`, `secrets.token_urlsafe(24)` at `:30-31`), imported function-locally in `start()` the way `bobi/service.py:628` and `:685` already import. Written next to the port file in `start()` (`:250-251`), unlinked with it in `stop()` (`:271-273`). Sent as a request header, following `bobi/webui_common/security.py:10` and the compare at `:70`.
- `POST /subscriptions/reload`, token required. Looks up `get_live_subscription(manager_session)` and calls `reload(authorize=True)`.
- `GET /subscriptions`, token required. Returns the in-memory accepted record without PUTting, for `list` (D11).
- `GET /health` and `GET /ready` stay unauthenticated, so Kubernetes and Fly probes are unaffected.

The token is not optional and it is not the UI's.
`BOBI_HEALTH_BIND=0.0.0.0` ships in the reference image (`docs/REFERENCE_IMAGE.md:139`, `tests/test_self_hosted_consumer_assets.py:96`) and `tests/test_manager_health.py:60`, `:184` already reason about network reachability, so an unauthenticated mutating route would let anything on the network trigger credential-bearing authorize POSTs and state-changing PUTs.
The UI token is not reused because the UI is optional and has broader authority.

`_make_handler` (`:64`) gains `manager_session` and the token as closed-over values; `start()` (`:216-218`) already takes closed-over callables, so this is the same extension point.

Wire shapes, which is what the CLI's behaviour is specified against.

```json
POST /subscriptions/reload  ->  200
{"status": "applied",
 "subscriptions": ["inbox/eng-team", "github:moda-labs/bobi-agent", "session.completed"],
 "added_topics": ["github:moda-labs/new-repo"],
 "removed_topics": [],
 "at": "2026-10-10T18:22:04Z"}
```

```json
POST /subscriptions/reload  ->  200
{"status": "rejected", "error": "unauthorized_topics",
 "topics": ["github:moda-labs/new-repo"]}
```

```json
POST /subscriptions/reload  ->  200
{"status": "rejected", "error": "deployment_auth_failed", "http_status": 403}
```

```json
POST /subscriptions/reload  ->  200
{"status": "skipped", "reason": "degraded_detection",
 "subscriptions": ["inbox/eng-team", "github:moda-labs/bobi-agent"]}
```

```json
POST /subscriptions/reload  ->  409
{"status": "no_live_subscription", "session": "eng-team"}
```

```json
GET /subscriptions  ->  200
{"session": "eng-team",
 "subscriptions": ["inbox/eng-team", "github:moda-labs/bobi-agent"],
 "at": "2026-10-10T18:22:04Z"}
```

A 2xx means the manager processed the request; the body says what the EVENT server answered, so the CLI never has to tell "the route refused me" apart from "the topics were refused".
Non-2xx is the route itself: `401` for a missing or wrong token, `409` for no live subscription, `503` if the manager is shutting down.
`added_topics` and `removed_topics` are computed by the manager by diffing the new set against its in-memory accepted record, because the server returns only numeric counters (`core.ts:1537-1540`).
`at` is the apply time of the returned set, which is what D4 labels "last accepted at T".
`deployment_auth_failed` is deliberately NOT named `deployment_unknown`: `authenticateDeployment` (`core.ts:783-790`) returns the same `403` (`:1491-1492`) for an evicted deployment, a wrong deployment id and a wrong api key, so the route must not claim to know which. The CLI's wording follows the existing boot message at `bobi/subagent.py:1901-1905`.

**The reload is bounded, and the CLI's timeout is derived from that bound.**
`_HANDLER_TIMEOUT = 10` (`bobi/manager_health.py:33`, applied at `:73`) is a socket READ timeout, not an execution deadline, so it does not bound the handler's work.
The work itself is bounded: `authorize_resources` loops serially over global topics (`bobi/events/server.py:740-784`) with `timeout=10.0` per call (`:650-653`), then one `put` with `timeout=10.0` (`bobi/http.py:78`).
Worst case is `10s x (number of global topics) + 10s`, which is about 80s on this team.
The CLI's POST timeout is set ABOVE that bound and the spec states it, so a slow authorize cannot make the caller give up on an apply that then lands. The remaining indeterminate case is a dead socket, which is risk R4.

### The CLI

```
bobi agent <name> subscriptions list
bobi agent <name> subscriptions add <topic>...
bobi agent <name> subscriptions remove <topic>...
```

Generic by Zach's own test: the command names a framework concept (a topic), not a domain one (a repo).
It follows the `monitors` group precedent (`bobi/cli.py:2974`, registered at `:3292`).

`<name>` is the INSTALLED slot, not the pack's `agent:` key.
This deployment is installed as `eng-team` while the pack declares `moda-eng-team`, so the director must derive it from `BOBI_ROOT` via `paths.agent_name_for_root` (`bobi/paths.py:92`) rather than guessing.
D1 means the director agent is the primary caller, from inside the runtime; `_detect_project_root` (`bobi/cli.py:80`) honors the inherited `BOBI_ROOT` that the `agent` group binds.

`add` and `remove` each:

1. Resolve the runtime root via `_detect_project_root`.
2. Normalize and validate every topic argument BEFORE any write: strip, reject an empty or whitespace-only argument, reject a duplicate within the argument list, and reject any `inbox/` or `reply/` topic.
   It calls the workspace validator's own rules rather than restating them, so the CLI cannot write a file its own reader rejects and fail every later apply, and the namespace ban has exactly one definition.
   Nothing else is rejected by namespace. `agent/auto_dispatch.failed` is a real published topic (`bobi/events/reactor.py:382`, documented at `docs/EVENT_SERVER.md:371` and `docs/MONITORS.md:256`), so an `agent/` prefix ban would silently narrow D1.
   A rejected argument fails the whole command non-zero with nothing written.
3. Compute the effective list the edit applies to when the file is absent: `discover_subscriptions(project_path)`, so a team that was auto-detecting keeps its effective set instead of collapsing to the one topic named on the command line.
   On an auto-detecting team that makes live Slack and Linear API calls (`bobi/events/adapters.py:290-315`), and the command says so in its own output.
4. Under the existing blocking `fsutil.file_lock` (`bobi/fsutil.py:162`, `LOCK_EX` at `:180`), re-read the file, apply the topic edit, and write it back with `atomic_write_text` (`:100`), per `AGENTS.md:93` and `:97`.
   The re-read inside the lock is what makes two concurrent commands compose instead of overwriting each other; step 3's value is used only when the file is still absent.
   `add` is idempotent. `remove` requires the topic to be present and otherwise exits non-zero with nothing written.
   No timeout is needed on this lock, because no network call happens inside it.
5. Read `state/manager-health.port` and `state/manager-health.token` and POST `/subscriptions/reload` to `127.0.0.1:<port>`, with a timeout above the route's own worst-case bound.
   An absent port file, a refused connection, or a `409` means no live subscription: say plainly that the change is persisted and not yet live, name that it applies at the next reload or manager start, and exit ZERO.
   This is the whole liveness test. It is self-evidencing, so there is no pid probe, no launch stamp and no state-file table.
6. Report the outcome from the response body.
   `applied`: print `added_topics` and `removed_topics` in full, not just the topic typed, because D12 means a reload can also apply monitor drift.
   `rejected` with `unauthorized_topics`: print the topics the server named, say the workspace change was kept, and exit non-zero (D7).
   `rejected` with `deployment_auth_failed`: report what the server's 403 actually means, which is a deployment the server does not recognise with this credential, name eviction after a long stop as the likely cause without asserting it, say the change is persisted and that recovery is an explicit operator action (D10), and exit non-zero.
   `skipped` with `degraded_detection`: say source detection returned a narrower set than the live one so nothing was applied, and exit non-zero.
   `401`: say the token did not match and name `state/manager-health.token`, and exit non-zero.

`list` prints three things:

1. The declared list, labelled as persisted, or as the pack's or auto-detected when no workspace file exists yet, saying when that resolution made live Slack or Linear calls.
2. The accepted set from `GET /subscriptions`, labelled "last accepted at T" per D4.
3. Topics in the declared list that are absent from the live set, which is what a rejected or not-yet-applied topic looks like.

It flags that one direction only.
Derived keys and `--subscribe` extras are in the live set by design and never in the file, so flagging every difference would fire on every run of a healthy team.
With the manager down, or no live subscription, it prints the declared list and says the live set is unavailable because the manager is not running (D11).
It never PUTs: `core.ts:1522-1536` would make that a write.
Per D8 it makes no claim about current routing at the event server; the reconnect re-assert remains that repair.

## Concurrency, in one section

Five pieces of state, and one writer each.

| # | State | Location | Writer |
| --- | --- | --- | --- |
| S1 | pack `subscribe:` seed | `<run>/package/agent.yaml` | the pack author; read-only at runtime (`bobi/runtime_guard.py:143-153`) |
| S2 | declared list | `<run>/workspace/subscriptions.yaml` | the CLI, under `file_lock`. A hand edit is possible and is risk R2 |
| S3 | derived keys | computed per apply, never stored | the composer |
| S4 | accepted set and its timestamp | the owning session's memory | that session, under the module lock. Read by `GET /subscriptions`, never replayed as desired state |
| S5 | routing index and deployment identity | the event server | the owning session's PUT, plus the server's own eviction sweep |

Two CLI commands racing: each does read-modify-write under `fsutil.file_lock`, so S2 lands at `A+X+Y`.
Each then POSTs a reload, and the manager RE-READS S2 rather than applying a caller's delta, so whichever reload runs after the second write PUTs `A+X+Y`.
Reversed arrival order is harmless: the earlier reload PUTs a set that already contains both topics, and the later one is a no-op at the server (`added: 0, removed: 0`).
That is linearizable without a generation counter, because the two writers write different things: the CLI writes S2, the owning session writes S4 and S5.

Boot against reload against reconnect is handled by the lock scope and the registration order stated under "Reload, and the one lock", and is not restated here.
What matters at this level is the consequence: `replace` is a full set, so it is idempotent, and every recovery path in this design is a retry of the same operation rather than a compensating action.
The six-row liveness table the previous design needed is gone because the window it described is closed by ordering rather than detected.

A manager start remains the reconciliation point for any divergence, PROVIDED its deployment identity still exists on the server.
Both server implementations evict a deployment whose WebSocket has been gone longer than 60s (`event-server/src/local.ts:77`, sweep at `:818-845`; the Worker's DO alarm at `event-server/worker/src/deployment-session.ts:237`, `:245-286`), and today's boot deliberately keeps the stale credential rather than re-register (`bobi/subagent.py:1901-1906`, `:1917-1920`).
D10 keeps that, so the CLI names eviction from the 403 in the reload response instead of promising a start will apply the change.

## Exact changes per file

### `moda-labs/bobi-agent`

**`bobi/events/subscriptions.py`** (~120 lines)
Add `workspace_subscriptions(project_path) -> list[str] | None` with the strict validator, including the `inbox/`/`reply/` ban, exported so the CLI reuses the same rule; it does not reuse `_normalize_explicit_subscriptions` (`:13`) and it does not interpolate.
Add `WorkspaceSubscriptionsError(path, reason)`.
Add `compose_session_subscriptions(project_path, session, extra=())`, the move of `bobi/service.py:630-647` plus `bobi/session.py:1657-1660`, with `extra` applied for every session and the workspace, monitor and lifecycle layers gated on the manager session.
Have `explicit_subscriptions` (`:25`) return the workspace list when it is not `None`.
Have `discover_subscriptions` (`:51`) consult `workspace_subscriptions` before and outside the `try` at `:59-66`, gating on `is not None`.
Leave the pack gate at `:61` unchanged.

**`bobi/subagent.py`** (~120 lines)
Add a module `threading.Lock` and the live-subscription registry (mirroring `bobi/inbox.py:76-92`).
`_start_event_subscription` (`:1715`) creates and registers the controller as its FIRST act, before the compose and before `:1957-1960`, and unregisters in a `finally` if the boot raises; the controller holds the `reload(*, authorize)` closure, the deployment identity filled in at `:1957-1960`, and the in-memory accepted record.
`_register_with_retry` (`:1811`) and `_sync_saved_deployment` (`:1856`) compose and apply inside one lock acquisition each, and seed the record on success.
`_resubscribe_on_deaf` (`:1974-1982`) calls `reload(authorize=False)`, which carries the degraded-detection guard; `active_subscriptions` is deleted.
`_put_subscriptions` (`:1926-1946`) raises `UnauthorizedTopics` before `:1943` and returns the body's `subscriptions` array, or the PUT list when the body omits one.
The classifier (`:1894-1924`) gains an `UnauthorizedTopics` branch.
`Subscription.stop` (`:1662`) unregisters.
Keep `filter_unauthorized=False` at `:1883`.

**`bobi/cli.py`** (~130 lines)
Add the `subscriptions` group with `list`, `add`, `remove`, then `main.add_command(subscriptions)`.
Add `"subscriptions"` to the agent-group list at `:4305-4307`, without which `bobi agent <name> subscriptions` does not resolve, and to the pop list at `:4313-4319` so it is not also a top-level command.
Import `bobi.service` lazily for `manager_session_name` as `:53` and `:1232` already do.

**`bobi/manager_health.py`** (~70 lines)
Add the token (minted by `write_secret`, written in `start()` at `:250-251`, unlinked in `stop()` at `:271-273`), `do_POST /subscriptions/reload`, `GET /subscriptions`, and the two closed-over values on `_make_handler` (`:64`).
`do_GET` (`:75-83`) keeps `/health` and `/ready` unauthenticated.
`_HANDLER_TIMEOUT` (`:33`) is left alone: it is a socket read timeout and the reload's own bound is what the CLI's timeout is derived from.

**`bobi/service.py`** (-18/+8)
Delete `:630-647`. Pass the raw `--subscribe` extras at `:764`.

**`bobi/session.py`** (-4/+1)
Delete the `inbox/<self>` prepend at `:1657-1660`; pass `self._subscribe` through at `:1673` and `:1710`.
`tests/test_session.py:907` asserts the background retry receives `["inbox/test-wake", "github:o/r"]` and must be updated to the extras-only argument in this change; it is a characterization test of the old prepend site, not a behaviour regression.

**`bobi/doctor.py`** (~5 lines)
`_check_ingress_reachability` (`:663`) gains `except WorkspaceSubscriptionsError` returning `ok=False` with the path, ahead of `:675` and `:678`, both unchanged.

**`bobi/events/client.py`** (~4 lines)
Fix the stale "(and re-registers on failure)" clause at `:501-502`: only `_register_with_retry` re-registers, while `_sync_saved_deployment`'s PUT error path retains the deployment and cursor and says so at `bobi/subagent.py:1917-1920`.
Raise the swallowed-exception log at `:519` from `debug` to `warning`, so a voided repair PUT is visible.

NOT touched, versus the previous design: `bobi/events/state.py` (no accepted-record API), `bobi/fsutil.py` (no lock timeout), `bobi/events/server.py` (no `put_subscriptions` extraction), `bobi/state_version.py` (no new state file to version), `bobi/launch_stamp.py` (no liveness table).
The three deployment-record shape assertions (`tests/test_event_subscription.py:71`, `:471`, `:515`) need no attention, because nothing here writes that document.

**Docs** (~40 lines)
`docs/EVENT_SERVER.md:392-396`: the resolution order gains a level above "Explicit", plus the reload route, its token, and the eviction consequence for a persist-only edit.
`docs/BUILDING_AGENT_TEAMS.md:129-133`: `subscribe:` stops being the live source of truth once a workspace file exists, and omitting it still means auto-detection.
`skills/bobi.md`: the new CLI group, which `AGENTS.md:12` names as the CLI command reference.
`docs/REFERENCE_IMAGE.md`: the token file beside the port file.
`AGENTS.md:133` requires all of these in the same PR, never as a follow-up.

Roughly 500 lines of code across seven files plus docs.

### `moda-labs/moda-agents` (companion PR)

**`agents/moda-eng-team/agent.yaml`**
Remove the `managed_repos:` key and its entire block, from `:151` through EOF at `:162`, as a block rather than a line range so the edit cannot half-apply and orphan `:161-162` into invalid YAML.
Leave `subscribe:` (`:127-134`) in place: it is the seed, and removing it would strand a fresh deployment with nothing to seed from.
Update the header comment (`:103-126`) to say the list is a seed, not the live source of truth, and drop the now-wrong redeploy sentence at `:124-126`.

**`scripts/check-deploy-compose.py`**
Drop the `managed_repos` assertion at `:85-94`, which cannot pass once the key leaves the pack.
Keep the `subscribe:` assertion at `:76-83`: the seed must still carry moda-skills.

**`agents/moda-eng-team/workspace/managed-repos.yaml`** (new)
The director-maintained tracker bindings, carrying the content removed from `agents/moda-eng-team/agent.yaml:151-162`.
Seeded by the existing `seed_workspace` (`bobi/install.py:174-192`), which copies pack `workspace/` templates only if absent, so a later install never overwrites director edits.
Four packs already ship a `workspace/` directory through that path, so the mechanism is proven.
`subscriptions.yaml` is deliberately NOT shipped this way, per D6.

**`agents/moda-eng-team/roles/director/ROLE.md`**
Point `:12` and `:22` at `workspace/managed-repos.yaml` instead of the unnamed "configuration", and say the director keeps that file current.
Resolve the agent name from `BOBI_ROOT` rather than a literal `<name>` in any command the prompt prints.

**`agents/baohua/`** (comment-only)
`agents/baohua/agent.yaml:105` and `:118`, and `agents/baohua/README.md:152`, describe the pack `subscribe:` short-circuit as the top of the resolution order.
Update the prose; baohua's behaviour is unchanged, since its pack list is still its seed.

## Test plan

Twenty tests.
The risk lives in the real event server, in cross-session isolation and in the recompose paths, so the load-bearing tests are integration tests where the harness can reach them and unit tests on the real seam where it cannot.

Each is an ACCEPTANCE test, red against pre-change code for the right reason, unless labelled CHARACTERIZATION.

**Integration, against the real local event server.**
The fixture is `tests/integration/test_event_server.py:371` (`event_server`), with `:394` for a deployment and `:62` (`_seed_resource_grants`) for grants.
`tests/integration/test_e2e_event_flow.py:87` already boots a real manager against a real event server, and `tests/integration/test_event_isolation.py:211` already runs two real session subprocesses against one server.

1. A CLI `add` makes the server route the new topic and a CLI `remove` stops it, with no restart, and the deployment id and api key unchanged across both. Proves identity preservation and that no `register()` happened.
2. After an `add`, the live set still contains every monitor key, both forms of both lifecycle keys, and `inbox/<manager session>`: a published `agent/session.completed` is still delivered and `bobi agent <name> message` still reaches the manager.
3. Cross-session isolation survives recomposition: on the `test_event_isolation.py:211` harness, a worker's deployment never gains a `github:` topic while the manager's CLI change is applied, and the worker's own reconnect PUTs only its inbox.
4. Two concurrent `add` calls from a deterministic barrier where both read the same initial file: both topics in the file, both routing on the live server, and both commands exit zero.
5. D7 end to end: `add` of an ungranted `github:` topic returns `400 unauthorized_topics`, the CLI prints that exact topic list and exits non-zero, the workspace change is kept, and the previously accepted set still routes. Also pins the uncovered server behaviour: `event-server/test/core.spec.ts` asserts `unauthorized_topics` only for the register path (`:2030`), never for `handleUpdateSubscriptions` (`:2199`).
6. Eviction and D10: stop the manager, let the threshold elapse on the fast-eviction fixture (`tests/integration/test_inbox_transport.py:249`), start it, and assert the PUT gets `403`, the deployment record and cursor are retained (`bobi/subagent.py:1917-1920`), and a previously persisted `add` is therefore NOT live. A second leg corrupts the saved api key instead and asserts the CLI reports the SAME diagnosis, because `authenticateDeployment` (`core.ts:783-790`) collapses both into one `403` and the wording must not over-claim.
7. The CLI against a booting manager: with the pid published but the session not yet registered, `add` gets `409`, reports persist-only, exits zero, and the topic is live after that boot completes. The version that snapshots at `bobi/service.py:630-647` loses the topic, which is what this test catches.
8. D12: pause a monitor after boot, then `add` a topic. One PUT both adds the topic and drops the paused monitor's two keys, and the command's output names both changes.
9. A direct file edit writing `inbox/<worker>` (and a second leg, `reply/x`) is rejected by the reader, so the next reload performs NO PUT, the boot compose fails with the file path, and the worker's events never reach the manager. This is the only test that reaches the hand-edit path the CLI's own argument check cannot cover.
10. A persistent worker launched with `--subscribe github:org/repo` (`bobi/cli.py:3475-3476`) keeps that topic across a deaf reconnect and never gains a `github:` topic that is only in the manager's workspace file. Proves `extra` survives the composition move for a non-manager session.

**Unit, on the recompose paths.**
The deaf path is entered only after 95s of no pong (`bobi/events/client.py:260`, `:294`), so these drive `on_deaf_reconnect` off a patched client exactly as `tests/test_event_subscription.py:110-136` already does, with no wait.

11. A manager's deaf reconnect recomposes from the file: with the file changed after boot it PUTs the new set, not the boot-time list. A second leg drops the PUT response on the CLI's apply and asserts the next reconnect still PUTs the NEW set, which is the revert this design removes.
12. A worker session's deaf reconnect PUTs only `inbox/<worker>` plus its own extras, with a manager workspace file also on disk, so it proves the composer keyed off the session name. Extends `test_deaf_reconnect_uses_filtered_registered_subscriptions` (`tests/test_event_subscription.py:110`).
13. `compose_session_subscriptions` for a non-manager session returns `["inbox/<name>"] + extra` with no monitor or lifecycle keys and no network call, against a real detector rather than a mock. This is the regression guard for every existing direct caller of `_start_event_subscription`.
14. The lock covers compose AND apply, driven by a deterministic barrier: pause reload A inside the lock after it composes, mutate the file, fire the deaf reconnect, release A, and assert the final server set equals the newest file contents and that the two applies did not interleave. A version that locks only the PUT lands A's stale set and is caught here.
15. The boot window, as a unit test on ordering: a reload POST issued after the controller is registered but before the boot's apply at `bobi/subagent.py:1957-1960` blocks, then PUTs the post-edit set. The pre-registration case returns `409` and the boot's own compose carries the change. Together with integration test 7 this is the whole of what the previous design's six-row liveness table covered.
16. The degraded-detection guard: on a team with no workspace file, Slack and Linear detection raising (`bobi/events/adapters.py:201-205`, `:313-315`) makes `discover_subscriptions` return the narrower fall-through, and the reconnect then performs NO PUT and logs at `warning`, leaving routing intact. A second leg proves a shrink WITH a workspace file present is applied, because that is a legitimate operator removal.
17. `UnauthorizedTopics` reaches the caller with the topic list, and `_sync_saved_deployment`'s classifier still reports "resource grants rejected (HTTP 400)" (`bobi/subagent.py:1910`) rather than the `:1916` catch-all, and the deaf path logs it at `warning` (`bobi/events/client.py:519`).

**Unit, on the subscription surface and the route.**

18. The strict validator, one case each: unparseable, empty file, `{}`, missing `subscribe`, non-mapping root, bare-string value, non-list value, non-string element, duplicate topic, unknown top-level key. All raise `WorkspaceSubscriptionsError` naming the fault and none auto-detects, asserted against the real detector so the test fails if the read is ever moved inside the `try` at `bobi/events/subscriptions.py:59-66`. The same test asserts `bobi/supervisor/snapshot.py:105-108` still returns an empty expectation list rather than raising, and that `bobi doctor`'s ingress check reports `ok=False` with the path.
19. The precedence pin: with a workspace file present, `explicit_subscriptions` and `discover_subscriptions` agree, extending the parametrized `tests/test_ingress.py:260-276`. Cases: absent file (`None`), `subscribe: []` (returns `[]`, red against the `:61` gate), and a non-empty list. Plus: the first `add` on a team with no file seeds from the effective set and writes resolved topics.
20. The route: POST `/subscriptions/reload` without the token is `401` and triggers no PUT; with it, it reloads and returns the server's body; `GET /subscriptions` without the token is `401`; `GET /health` and `GET /ready` stay unauthenticated; and the token file is mode 0600 and unlinked by `stop()`. CLI-side argument validation rides here too: `add inbox/<worker>`, `add reply/x`, `add ''`, a duplicated argument, and `remove` of an absent topic each exit non-zero with nothing written and no POST.

Four assertions are deliberately NOT new tests, each already covered.
`replace` returning the accepted set: `event-server/test/core.spec.ts:2284-2306` (`added: 1`, `removed: 1`, the exact array at `:2301`) and over real HTTP at `tests/integration/test_event_server.py:510-518`.
`{"replace": []}` returning 400: `core.spec.ts:2314-2324`.
A `remove` whose survivors were granted at an earlier boot: `core.spec.ts:2284-2306` seeds the grant before the replace and it passes, and grants carry no TTL.
The server storing an arbitrary `inbox/` key, which is why step 2 rejects the namespace: `core.spec.ts:2251-2262`.

## Accepted risks

Per D13, these are named and left uncovered rather than engineered around.
Each carries the cost of covering it, for discussion.

**R1. The server's `replace` is not failure-atomic after validation (D9).**
The authorization gate rejects the whole update before any write (`core.ts:1509-1512`), so an ungranted topic never half-applies.
After that, `replace` runs independent storage operations in sequence with no transaction: the interface exposes `putDeployment`, `addSubscription` and `removeSubscription` separately (`core.ts:288-291`) and the loop calls them one after another (`:1522-1536`).
Likelihood: needs a storage failure inside a single Durable Object request.
Impact: routing half-changed while the deployment record keeps the old set. The symptom is behavioural, since D8 leaves no API to read the index; the local signal is that the PUT raised.
Recovery: re-run the command, or restart the manager, both of which are a full idempotent `replace`.
Cost to cover: making the event server's `replace` transactional, which is a separate ticket in a different service.

**R2. A VALID hand edit of `subscriptions.yaml` can be overwritten by a concurrent command.**
`<run>/workspace/` is writable by design (only `<run>/package/` is guarded, `bobi/runtime_guard.py:143-153`) and is advertised to the agent brain as its own scratch space (`bobi/prompts/resolver.py:86-95`), so neither a human nor the director can be prevented from editing the file directly.
Such an edit takes no lock, which is exactly what `bobi/fsutil.py:169-172` warns about.
An INVALID hand edit is not in this risk: the strict validator fails it loud at every read, and a forbidden `inbox/`/`reply/` topic is rejected there too, so the dangerous content classes cannot pass.
What remains is a well-formed edit landing inside a command's read-modify-write.
Likelihood: low, and it needs the edit to overlap a command.
Impact: one lost edit, visible in `list` and fixed by re-editing.
Cost to cover: nothing can force a third-party writer to take an advisory lock. The file header says the CLI is the supported editor.

**R3. A rejected topic left in the file poisons later reloads.**
D7 keeps an ungranted topic in S2, and because the manager recomposes rather than applying a delta, every later reload carries it and 400s until it is removed.
Likelihood: whenever a grant is missing, which is the normal case for a repo added before its GitHub App install.
Impact: a later valid `add` does not apply live while the bad topic sits there. The team keeps working on its previously accepted routing, and the boot classifier names the topics (`bobi/subagent.py:1910`).
Recovery: `subscriptions remove <topic>`, which `list` points at by flagging the topic as declared and not live.
Cost to cover: a per-topic partial apply, which is the delta machinery this design deleted.

**R4. A dead socket mid-apply leaves the caller unable to tell whether the change landed.**
The reload is bounded and the CLI's timeout is set above that bound, so a slow authorize cannot cause this.
A connection that dies while the manager is mid-apply still can: the manager finishes and the CLI never reads the answer.
Likelihood: low. It needs the loopback connection to break inside an operation of at most ~80s, on a host where the CLI and the manager are the same machine.
Impact: the operator does not know whether routing changed. Nothing is corrupted, because the apply is a full idempotent `replace`.
Recovery: `subscriptions list` reads the accepted set from the manager, and re-running the same command is safe.
Cost to cover: an asynchronous reload with a result id the CLI polls, plus the result bookkeeping to go with it. That is a request-tracking protocol for a case a second `list` already answers, which is exactly what D13 says not to build.

**R5. A pack `${VAR}` in `subscribe:` stops being re-interpolated once the file exists.**
The CLI seeds resolved topics and the workspace reader does not interpolate.
Likelihood: no fleet pack uses `${}` inside `subscribe:` at `bf9d1088`, so this is latent rather than live.
Impact: a topic that used to follow an env var is frozen at its value when the first `add` ran.
Cost to cover: interpolate in the workspace reader too, which then makes a literal `$` unwritable and gives the file two different parse contracts for one key.

## Migration

No behaviour change on upgrade, for any pack shape, because nothing writes a workspace file until an operator runs `add` or `remove`.
Until then every pack resolves its subscriptions exactly as it does today: a pack list short-circuits, and a pack without one auto-detects.

The composition move is behaviour-preserving by construction: the manager session composes the same four layers in the same order, one call frame later.
The deployment id, api key and event cursor are untouched: nothing here calls `register()`, nothing unlinks a cursor, and the credential record is not rewritten.
Before the first manager restart on the new version, a `subscriptions add` finds no reload route, reports persist-only and exits zero, and the change applies at that restart.

### Release ordering

The fleet's bobi version is pinned in `moda-labs/moda-agents` `.github/fleet-version` and moved automatically by `version-gate.yml` once `ci-canary` proves a release.

1. Land and release the bobi-agent change; let the canary prove it and the pin PR land.
2. Land the moda-agents companion PR.
3. Roll the fleet.

Step 2 must not precede step 1: the director prompt would otherwise name commands a fleet on the pre-change pin does not have.

## Out of scope

- A GET route on the EVENT SERVER, and any server-side introspection. D8 settles it. The `GET /subscriptions` added here is on the manager's own local health server, not the event server, and it is what D11 requires.
- Making a `whatsapp:`, `discord:` or new-workspace `slack:` topic apply LIVE from the CLI. Their grants are written by the channel registrations the manager runs at session start (`bobi/subagent.py:1767`), which a reload does not re-run, so such a topic persists and applies at the next start. An in-workspace `slack:` channel does apply live, because the grant is keyed on the team id.
- Making the server's `replace` transactional. R1 accepts the window and states the recovery.
- Recovering an evicted deployment automatically. D10 settles it: explicit operator action, as today.
- Hot-reloading anything else from the workspace. A reload converges the composed subscription set and nothing more.

## Open questions

None.
D1 through D9 were settled on 2026-10-09 and D10 through D13 on 2026-10-10.
The items that would otherwise be open are in "Accepted risks" with their cost of coverage, per D13, as discussion items rather than blockers.

## Amendment, 2026-10-10 (implementation)

One spec defect, found by the codex review of the implementation and fixed in
the implementing PR (#1116). The design is unchanged; one predicate is.

**The degraded-detection guard's test was wrong.** "Reload, and the one lock"
specifies that `reload(authorize=False)` refuses to PUT when there is no
workspace file AND the recomposed set is "a strict subset of the last accepted
set". A strict subset is the wrong test for the hazard this spec itself names.
When auto-detection fails, `discover_subscriptions` falls through to
`[project_path.name]` (`bobi/events/subscriptions.py:78` at `f712372d`), which
ADDS a topic. A Slack-only team whose detector failed therefore recomposes to a
set that is NOT a subset of what it had, the guard does not fire, and the
reconnect PUTs it and drops Slack routing - exactly the outcome the guard exists
to prevent.

The implemented test is: refuse when there is no workspace file AND the
recomposed set LOSES any topic in the last accepted set. Same three lines, same
state, and it covers the fall-through the original predicate let through. With a
workspace file present a loss is still a legitimate operator removal and is
applied, unchanged.

Two further notes, neither a design change:

- The reload token is created at mode 0600 rather than minted by
  `webui_common.launcher.write_secret`, which writes the file and chmods it
  afterwards. "The reload route" names `write_secret`; `CLAUDE.md` names this
  exact exception to the fsutil rule, with `events.state.save_bubble_state` as
  the precedent. Here the token is the only boundary for a server that may bind
  `0.0.0.0`, so a window at 0644 is not acceptable.
- R4's bound is weaker than stated. "The CLI's POST timeout is set ABOVE that
  bound" holds for one caller; every reload serializes on the apply lock, so a
  queued caller can wait out another reload's full bound first. The
  implementation budgets for one queued apply and, when the budget does expire,
  reports that the apply may still have landed and points at `subscriptions
  list` - which is R4's own recorded recovery - rather than claiming the manager
  is down.

**One item goes back to the director, not decided here.** "The CLI" states that
an absent port file or a refused connection "is the whole liveness test. It is
self-evidencing". It is not quite: `manager_health.stop()` unlinks the port and
token files only on a GRACEFUL stop, so a SIGKILLed, OOM-killed, or host-lost
manager leaves both behind. The CLI then POSTs the token to whatever later bound
that port. The outcome is safe (an unrecognised response exits non-zero), but
the token does leave the process. Closing it costs one authenticated `GET
/health` before the POST, reusing `manager_health.health()`. That is new
machinery the spec deliberately excluded, so it is recorded here rather than
added.
