# Workspace-owned event subscriptions

Issue: [#952](https://github.com/moda-labs/bobi-agent/issues/952)
Status: spec, awaiting approval (Gate 1).
Written 2026-10-09 from scratch, replacing the generic-overlay design in PR #956.

Every `bobi/`, `event-server/`, `tests/`, `docs/` and `AGENTS.md` file:line below was read at `83bebe49` (`origin/main`, 2026-10-09).
Every `moda-agents` file:line was read at `bf9d1088` (`origin/main`, 2026-10-08).
Line numbers from the August review rounds have moved and are not reused.

Revision 4, after three independent review rounds:
- `plans/reviews/2026-10-09-952-subscriptions-review-1.md` (3 blockers; the design lost its composition function as a result).
- `plans/reviews/2026-10-09-952-subscriptions-review-2.md` (2 blockers; the accepted set moved off the credential record, and seeding stopped firing for auto-detecting teams).
- `plans/reviews/2026-10-09-952-subscriptions-review-3.md` (1 blocker; the design lost its boot-time seeder, and the spec got 29 lines shorter while folding that blocker and four majors).

The design has gotten SMALLER at every revision.
Revision 1 had a composition function and a `bobi/service.py` refactor; revision 4 has neither, and no boot-time seeder either.

## Problem

Changing which repos a running team hears about means editing agent pack source in a second repository, rebuilding the pack, reinstalling it, and restarting the manager.

The topics live in `subscribe:` in `moda-labs/moda-agents` at `agents/moda-eng-team/agent.yaml:127-134`, installed as a frozen read-only image at `<run>/package/agent.yaml`.
The 2026-08-03 familystories-ai offboard is the measured case: a commit in another repo, a pack rebuild, an install, and a restart.
The pack comment at `agent.yaml:124-126` says so itself: "the server-side subscription set is replaced by `register` on the next manager-session start, so this team must be redeployed for the cut to take effect."

A team cannot change its own subscriptions, so onboarding is gated on a human doing a release in a different repository.

## Decisions

Settled by Zach in Slack `C0BAEN48KQR` thread `1791519568.830069` on 2026-10-09, quoted verbatim, not reopened here.

**D1.** The core question is generic: a running team may mutate its own event subscriptions.

**D2.** Subscriptions live as a list in the WORKSPACE; the pack `agent.yaml` `subscribe:` is only the SEED.

**D3.** A generic framework CLI subcommand applies changes, e.g. `bobi agent <name> subscriptions list|add|remove` (topics, not repos - no repo concepts in `bobi/`, and no `repos` CLI, per his Aug rulings).
It persists the workspace list and hot-applies via the existing identity-preserving `PUT /deployments/<id>/subscriptions {"replace":[...]}`.
No restart needed.

**D4.** Read-back of what is LIVE: `subscriptions list` shows the persisted list vs what the event server actually routes, flagging topics the server silently dropped.
Prefer reusing the PUT response if it already returns the accepted set; add a GET route only if needed.
Not via the admin channel.

**D5.** `managed_repos` is eng-team specific: remove it from the pack `agent.yaml`, move it into a director-maintained workspace file, and update the eng-team director ROLE prompt to read and keep it current.
Prompt/pack change only, no bobi-agent code.
Accepted trade-off: the director can grant itself branch-delete authority (Zach, Aug: branches are recoverable).

The old generic-overlay design (`overlay.yaml` merged over `agent.yaml`, `OverlayError`, framework-key rejection) is REPLACED, not amended.

## What the code actually does

### The live subscription set is per session, and it is not the workspace list

`bobi/service.py:625-642` composes the manager's topic list: the explicit list, then `--subscribe` extras, then `monitor_subscription_keys(...)`, then `lifecycle_subscription_keys()`.
That list reaches the session at `:759`.
`bobi/session.py:1657-1660` then prepends `inbox/<session name>` before registering, and `_start_event_subscription` (`bobi/subagent.py:1715`) registers exactly those keys.

So the live set is `["inbox/<session>"] + composed`, and `{"replace": [...]}` is authoritative: `event-server/core/src/core.ts:1522-1530` removes every stored subscription not in `desired`, and `:1535` assigns `deployment.subscriptions = desired`.

Two consequences govern the whole design.
A PUT built from the workspace list alone unsubscribes the manager from monitor findings, from sub-agent `session.completed`/`session.failed`, and from its own inbox, which is what makes it addressable by `bobi agent <name> message` and `ask`.
A PUT built by recomputing the composition also re-syncs whatever the monitor set happens to be at that moment: `bobi/cli.py:2965`, `:2992` and `:3016` write `package/monitors.yaml` at runtime under `with_mutable_runtime_package`, and the manager does not re-PUT on those, so recomposing from a CLI command would add or remove monitor topics as an undeclared side effect.

The design below therefore never recomputes the set.
It edits the set the server last accepted.

### The deaf-reconnect hook belongs to every session, not to the manager

`_resubscribe_on_deaf` is defined once inside `_start_event_subscription` (`bobi/subagent.py:1974-1982`) and wired into every client at `:1990`.
The only callers of `_start_event_subscription` are `bobi/session.py:1673` and `:1710`, which serve every session: the manager gets `["inbox/<self>"] + composed`, a worker gets `["inbox/<self>"]` only.

Today the hook replays the in-process `active_subscriptions` (`bobi/subagent.py:1765`, reassigned at `:1835` and `:1892`, read at `:1982`).
A CLI change applied to the server would be reverted by the next deaf reconnect.
But sourcing that hook from the manager's composed list would push the manager's `github:`/`slack:`/`linear:` topics onto a worker's own deployment on the worker's next deaf reconnect.
Per-session deployments exist precisely to prevent that: `bobi/events/state.py:21-25` and `tests/integration/test_event_isolation.py:1-16` record the 2026-06-12 incident where project leads received the user's Slack DMs to the director and replied to them.

The fix must keep the hook per session.

### The accepted set needs its own per-session file, not the credential record

`bobi/events/state.py:32-64` keeps two kinds of per-session state under `<run>/state/`: the deployment record (`deployment_state_path`, `:32`) holding `deployment_id` and `api_key`, and the event cursor (`session_cursor_path`, `:37`).
`save_deployment_state` (`:55-64`) replaces the whole document on every call, takes no lock, and reads nothing first.

The accepted set does NOT go in that record.
A CLI write to the credential document can lose a concurrent re-register's new `deployment_id`, and the next manager start then takes the `_sync_saved_deployment` branch, PUTs to a deployment the server deleted, gets 403, and boots into a background retry loop against the same stale record.
Three existing tests also assert that document's exact shape (`tests/test_event_subscription.py:71`, `:469-471`, `:514-515`), and the latter two assert it survives a PUT that does not re-mint.

A third per-session file follows the pattern the other two already set.
`event-server/core/src/core.ts:1537-1540` returns `{subscriptions, added, removed, protocol}` on a `replace`, and `bobi/subagent.py:1938-1946` reads that body, validates it, and throws it away.

The register path is different and must be described differently.
`register()` (`bobi/events/server.py:849-875`) returns `(deployment_id, api_key)` only; the response body (`core.ts:1473-1480`) carries no `subscriptions` member.
What a 201 proves is that the server stored the requested array verbatim (`core.ts:1461`), having rejected the whole request otherwise, so the register site records the list it passed in rather than a server echo.

### The empty-list fallthrough is a real hazard, and the malformed case is worse

`bobi/events/subscriptions.py:61` gates on `if explicit:`.
An empty list is falsy, so control falls through to `:68-78`, which calls `Config.load` and then `detect()`, and finally returns `[project_path.name]`.
An operator-emptied list does not mean "subscribe to nothing"; it means "auto-detect from git remotes".

Worse, the call at `:60` sits inside a `try` whose `except Exception: pass` at `:63-66` is deliberate and test-pinned by `tests/test_ingress.py:306-324`.
A malformed file read inside that `try` is swallowed and routes to the same auto-detection.

Measured at `83bebe49`, the fallthrough collapses this team's 4 GitHub topics to 1: `_detect_github` returns `['github:moda-labs/bobi-agent']`.
`bobi/events/adapters.py:139-142` names the Slack hazard in the same path ("swallowing it here would silently promote a channel-scoped subscription to a workspace-wide one"), and `_detect_linear` (`:290-315`) subscribes to every Linear team the API key can see rather than MDS and MOD.
The pack `subscribe:` is tolerable behind this gate because `<run>/package/` is read-only (`bobi/runtime_guard.py:143-153`).
The new file is not: `<run>/workspace/` is writable, is edited by the CLI by design, and is advertised to the agent brain as its own scratch space (`bobi/prompts/resolver.py:86-97`).

A malformed workspace file must therefore fail loud, not fall through.

### The ingress warning reads a different function, and `doctor` reads it too

`bobi/ingress.py:83` reads `explicit_subscriptions`, not `discover_subscriptions`.
`tests/test_ingress.py:260-276` pins the two to one parser, and the `explicit_subscriptions` docstring at `:28` names itself "The ONE parser for the `subscribe:` surface (D078)".

A workspace layer added only to `discover_subscriptions` would silently break that invariant while the test stayed green, because the test writes no workspace file.
The workspace layer goes inside `explicit_subscriptions`.

That also makes `bobi doctor` a reader, through `check_ingress_reachability` (`bobi/doctor.py:51`).

### Authorization is a network write, it fails open, and it covers two services

`authorize_resources` (`bobi/events/server.py:680-684`) is not a filter.
It POSTs the real GitHub/Linear credential to `/resources/authorize` once per global topic (`:650-655`, `:781-784`), needs the signed bubble identity from `<run>/state/bubble.json`, and returns the list UNFILTERED when that identity is absent (`:713-714`: "can't sign - leave the set unchanged").

It writes grants for two services only: `_RESOURCE_CRED_KEYS` at `:640` is `{"github": ..., "linear": ...}`, and `:761-765` passes `slack:`/`whatsapp:`/`discord:` through untouched because their grants come from the channel registrations in `_register_channel_credentials` (`bobi/subagent.py:1767`), which runs at session start.
The server checks all five prefixes (`core.ts:411`, `:1504-1512`), and the slack grant is keyed on the TEAM id rather than the channel (`core.ts:445-450`), so a new channel inside an already-registered workspace passes while a new workspace does not.

Its `filter_unauthorized=False` mode is the deliberate policy for an existing deployment, and both the function docstring (`:706-712`) and `_sync_saved_deployment` (`bobi/subagent.py:1859-1864`) give the same reason: "replacing the deployment's subscriptions with a filtered list would silently unsubscribe a valid existing deployment."
The CLI PUTs to that same deployment, so it uses the same mode.

Grants are permanent: `event-server/worker/src/index.ts:147-165` writes them with no `expirationTtl`, and `core.ts` has no revocation route.

### The server rejects; it does not silently drop

`core.ts:1508-1512` calls `unauthorizedGlobalTopics` and, on any ungranted topic, returns `400 {"error":"unauthorized_topics","topics":[...]}`, rejecting the whole update with no partial write.
`core.ts:1498-1500` returns `400 replace[] must not be empty`, so an empty replace is never a legal repair.

The only silent drop is client-side, in `authorize_resources` with `filter_unauthorized=True`, which only the register path uses: that is the parameter default at `bobi/events/server.py:682`, and the call site at `bobi/subagent.py:1805-1808` passes no keyword.
The new path uses `False`, so D4's "topics the server silently dropped" is always a 400 naming the exact topics, and no GET route is needed.

The register path already parses that 400 into `UnauthorizedTopics` (`bobi/events/server.py:627`, raised at `:840-842`).
The PUT path does not: `bobi/subagent.py:1938-1946` reaches `resp.raise_for_status()` first and discards the topic list.

### `managed_repos` has exactly one consumer

`grep -rn "managed_repos" bobi/` exits 1.
The only bobi-agent hits are `tests/test_memory.py:52,55,62,66`, where the string is arbitrary memory-index test data and not the pack key.

Fleet-wide there is one real consumer: `moda-labs/moda-agents` `scripts/check-deploy-compose.py:85-94` parses `managed_repos` off the composed pack `agent.yaml` and fails the deploy-compose check unless `moda-labs/moda-skills` maps to `github-issues`.
The director ROLE prompt is not a consumer today: `agents/moda-eng-team/roles/director/ROLE.md:12` and `:22` say "from configuration" and "in configuration" and never name the key, so `agent.yaml:155` ("Advisory tracker binding read by the director prompt") is already false.
D5's prompt half is an addition, not an update.

## Design

One new workspace file, one new CLI group, one new per-session state file.
No composition function, no change to `bobi/service.py`, no boot-time seeder, no new server route, no overlay, and no merge semantics.
`UnauthorizedTopics` is reused for the server's rejection, and the one new error class is for a malformed workspace file.

### The file

`<run>/workspace/subscriptions.yaml`, a mapping with one key:

```yaml
# Event topics this team subscribes to.
# Created by `bobi agent <name> subscriptions add`, seeded from the agent pack.
# Edit with `bobi agent <name> subscriptions add|remove`, which applies the
# change to the running event server without a restart.
# Comments below this header are not preserved across an edit.
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

A mapping under the same `subscribe:` key, rather than a bare YAML list, so the existing parser is reused rather than forked.
`explicit_subscriptions` already requires a dict and reads `raw.get("subscribe", [])` (`bobi/events/subscriptions.py:43-48`).
Keeping one parser for one wire shape is less new code and preserves the D078 invariant its docstring claims at `:28`.

Monitor topics, lifecycle topics and `inbox/<session>` are NOT in this file.
They are derived from `monitors:`, from the framework, and from the session's own identity, so writing them here would create a second source of truth that drifts.

The CLI writes a fixed header constant plus `yaml.safe_dump({"subscribe": [...]})`.
Only PyYAML is available (`pyproject.toml` declares `pyyaml>=6.0` and nothing else), and `safe_dump` discards comments, which the header says out loud rather than working around with line splicing.

### Presence, not truthiness

`bobi/events/subscriptions.py` gains one reader:

```
workspace_subscriptions(project_path) -> list[str] | None
```

`None` means the file is absent.
`[]` means the operator subscribes to nothing explicit, and is returned as such.
A malformed file raises `WorkspaceSubscriptionsError`, a new class in the same module, carrying the file path and the underlying parse error.
It is its own class rather than a bare `yaml.YAMLError` because `doctor` has to tell a malformed WORKSPACE file apart from a malformed pack `agent.yaml`, whose historical fall-through must not change.

`explicit_subscriptions` consults it first and returns it when it is not `None`, so the one parser keeps serving both its callers and the ingress warning at `bobi/ingress.py:83` follows the workspace layer for free.

`discover_subscriptions` calls `workspace_subscriptions` directly, BEFORE and OUTSIDE the `try` at `:59-66`, and returns the list when it is not `None`.
That placement is the whole point.
Inside the `try`, a malformed workspace file would be swallowed into auto-detection; outside it, the manager boot fails with the path, while a malformed PACK `agent.yaml` keeps its historical fall-through, which `tests/test_ingress.py:306-324` pins.
That test monkeypatches `explicit_subscriptions`, so `discover_subscriptions` must keep calling it inside the `try` rather than reaching past it to a pack-only reader; otherwise the pin stays green while no longer proving anything.

The file is read twice on an absent-file boot, once by each function.
One window follows: a file CREATED between the two reads is read from inside the `try`, so if it is also malformed the exception is swallowed for that one boot.
The CLI writes with `atomic_write_text`, so this needs a create rather than a torn write, and the next boot fails loud as intended.

The pack's own truthiness gate at `:61` is left alone, deliberately.
Nothing writes `package/agent.yaml` at runtime, so an empty pack list is not a reachable operator mistake, and `tests/test_symmetric_node.py:90-102` pins the current behavior.
(The `monitors` commands DO write `package/monitors.yaml` under `with_mutable_runtime_package`; `agent.yaml` has no such path.)

The three other readers degrade safely and are unchanged, except for `doctor`.
`bobi/supervisor/snapshot.py:104-109` wraps its `discover_subscriptions` call in `except Exception: pass` and is documented as best-effort, so a malformed file yields no declared subscriptions and silence detection simply asserts less.
`bobi/service.py:184-195` wraps `check_ingress_reachability` the same way, so the reachability warning is skipped at `log.debug`.
`bobi/doctor.py` must change: `_check_ingress_reachability` gains an `except WorkspaceSubscriptionsError` arm returning `ok=False` with the path, placed AHEAD of the two arms it already has (`except FileNotFoundError` -> `ok=True, "no agent config"` at `:673-675`, then `except Exception` -> `ok=True, "skipped: ..."` at `:676-678`).
Both existing arms stay exactly as they are; a missing config must keep reporting `ok=True`.
Without the new arm a malformed workspace file fails the manager boot while `doctor`, the command an operator runs first, reports green.

### Nothing seeds the file at boot

The file is created by the first `subscriptions add` or `remove`, and by nothing else.
There is no boot-time seeder, because none is needed: with no workspace file the reader returns `None`, `discover_subscriptions` falls through to the pack list exactly as it does today, and no auto-detection or network call happens.

```
$ cd /tmp && PYTHONPATH=<main worktree> python3 -   # pack with subscribe:, no workspace file,
                                                    # detect() monkeypatched to raise
module file: <main worktree>/bobi/events/subscriptions.py
explicit_subscriptions : ['github:moda-labs/lightweave', 'linear:MDS']
discover_subscriptions : ['github:moda-labs/lightweave', 'linear:MDS']
```

So a boot seeder would buy nothing a reader cannot already get, while costing a `bobi/service.py` change, a pack-non-empty gate (the pack reader cannot tell an absent `subscribe:` from `subscribe: []`, which is what made the first attempt at this write an authoritative empty list for most of the fleet), and three tests.
Creating the file lazily is also strictly better on upgrade: until an operator acts, there is no new file and no boot reads differently.

The CLI is therefore the only writer, and it holds `bobi/fsutil.py:162` `file_lock` across load, mutate and save, as `AGENTS.md:97` requires for read-modify-write state.
`atomic_write_text` alone keeps the file parseable without stopping a concurrent updater's change from being overwritten.

The cost is that `cat workspace/subscriptions.yaml` shows nothing until the first edit, so `list` carries a branch for it.
See Q1: if the file should exist from the first boot instead, the seeder comes back with its gate.

### The accepted set, recorded per session

A third per-session state file, beside the deployment record and the cursor:

```
<run>/state/subscriptions/<_safe_session(session)>.json  ->  {"subscriptions": [...], "at": "<iso8601>"}
```

`_safe_session` (`bobi/events/state.py:28-29`) is applied, as both sibling path functions do, because worker session names are caller-chosen (`bobi/subagent.py:836-838`).

`bobi/events/state.py` gains `accepted_subscriptions_path(project_path, session)`, `load_accepted_subscriptions(project_path, session) -> list[str] | None`, and `save_accepted_subscriptions(project_path, session, subscriptions)`.
The credential record is not touched, so the CLI never writes a file holding an api key and the three exact-shape assertions on it stay valid.

Two sites write it, both on the line where they already assign `active_subscriptions`:
- `_register_with_retry` (`bobi/subagent.py:1834-1835`) records the `authorized` list it passed to `register()`; a 201 proves the server stored that array verbatim (`core.ts:1461`).
- `_sync_saved_deployment` (`:1891-1892`) records what `put_subscriptions` returned, which is the server's own echo.

An absent or empty result is NOT recorded, and the reader returns `None` for a missing file, a missing `subscriptions` key, or an empty list.
That rule is load-bearing: `validate_server_response` ignores unknown fields and never checks `subscriptions` (`bobi/events/protocol.py:55-70`), nine existing tests answer the PUT with a bare `{"ok": True}`, and a recorded `[]` would make every later repair PUT a `{"replace": []}` that the server rejects forever (`core.ts:1498-1500`) with the exception swallowed on a daemon thread.

`_resubscribe_on_deaf` (`bobi/subagent.py:1974-1982`) reads its OWN session's record and PUTs that, falling back to `active_subscriptions` when the reader returns `None`.
A worker therefore replays `["inbox/<worker>"]`, and the manager replays whatever was last accepted, including a change the CLI made minutes ago.
That fixes the revert without a branch, without recomposition, and without any cross-session leak.
The read is a small JSON file on a daemon thread, written only with `atomic_write_json`, so a concurrent write reads as either the old or the new document and never a partial one.

A torn read is not the only race, so all three writers take `file_lock` on this file, and the CLI holds it across read, PUT and record.
Without that, an `add` that reads the record and then loses the CPU to a manager restart PUTs the pre-restart snapshot afterwards, removing whatever the restart's recomposed boot PUT had added: `bobi/service.py:625-642` recomposes from `MonitorRegistry.load(...)`, which `bobi/cli.py:2965`, `:2992` and `:3016` can have changed since.
That is the monitor re-sync trap arriving from the other direction.

No `CURRENT_FORMAT_VERSION` bump (`bobi/state_version.py:25-27`).
The file is new and optional, an older bobi never reads it, and a downgrade degrades to the `active_subscriptions` fallback rather than refusing to start.

### The CLI

```
bobi agent <name> subscriptions list
bobi agent <name> subscriptions add <topic>...
bobi agent <name> subscriptions remove <topic>...
```

Generic by Zach's own test: the command names a framework concept (a topic), not a domain one (a repo).
It follows the `monitors` group precedent (`bobi/cli.py:3155`).

`<name>` is the INSTALLED slot, not the pack's `agent:` key.
This deployment is installed as `eng-team` while the pack declares `moda-eng-team`, so the director must derive it from `BOBI_ROOT` via `paths.agent_name_for_root` (`bobi/paths.py:92`) rather than guessing.
D1 means the director agent is the primary caller, from inside the runtime; `_detect_project_root` (`bobi/cli.py:80-95`) honors the inherited `BOBI_ROOT` that the `agent` group binds.

`add` and `remove` each:

1. Resolve the runtime root via `_detect_project_root`.
2. Resolve the manager's session and its live identity, BEFORE any write, any network call and any process work: `manager_session_name(project_path)` (`bobi/service.py:135-144`), then `load_deployment_state(project_path, session)` (`bobi/events/state.py:44-52`) and `load_accepted_subscriptions(project_path, session)`.
   If either is missing there is nothing live to update, which is recorded now and changes only what happens after step 4.
3. Compute the effective list the edit applies to.
   When `workspace/subscriptions.yaml` exists, that is its contents.
   When it is absent, it is `discover_subscriptions(project_path)`, so a team that was auto-detecting keeps its effective set instead of collapsing to the one topic named on the command line.
   This runs OUTSIDE `file_lock`, because on an auto-detecting team it makes live Slack and Linear API calls (`bobi/events/adapters.py:290-315` POSTs to the Linear API with a 5s timeout), and the lock must not be held across a network round-trip.
   The command says in its own output that it made those calls.
4. Under `file_lock`, re-read the file, apply the topic edit, and write it back with `atomic_write_text` (`bobi/fsutil.py:100`).
   The re-read inside the lock is what makes two concurrent `add` calls compose instead of overwriting each other; step 3's value is used only when the file is still absent.
   Persisting is the whole operation, so if step 2 found nothing live, the command stops here, says plainly that nothing was applied live and that the change takes effect at next start, and exits zero.
5. Under `file_lock` on the accepted-set record, held from here through step 8: re-read the record, and abort with "the manager restarted, re-run the command" if it changed since step 2.
   Then build the PUT list by applying the SAME topic edit to the record.
   Nothing is recomposed, so monitor, lifecycle and `inbox/` topics pass through untouched by construction.
6. For `add` only, call `authorize_resources(..., filter_unauthorized=False)` on the NEWLY ADDED topics alone, so a new `github:` or `linear:` topic has its grant written before the PUT.
   Passing the whole PUT list instead would re-POST the real credential once per topic already granted (`bobi/events/server.py:781-784` authorizes per global topic), which is seven signed POSTs on this team where one is needed.
   The return value is discarded; `filter_unauthorized=False` is kept so the call can never narrow the set.
   If `load_bubble_state` (`bobi/events/state.py:79`) is empty the command FAILS with that fact, rather than proceeding into the unfiltered pass-through at `bobi/events/server.py:713-714`.
   `remove` skips this step entirely: the server re-checks every surviving topic (`core.ts:1509`), but grants carry no TTL and have no revocation route, so a survivor granted at an earlier boot still passes.
7. Resolve `es_url` from `cfg.event_server_url` with the `http://localhost:8080` fallback, and `ensure_running` when `local_port_from_url` matches, exactly as `bobi/subagent.py:1948-1955` does.
   Step 2's bail-out is what keeps a persist-only run from ever reaching this line and spawning a server.
8. `PUT /deployments/<id>/subscriptions` via the shared `put_subscriptions` helper, then record the returned set with `save_accepted_subscriptions`.
9. Print the full diff of the previous accepted set against the new one, naming every add and every remove, not just the topic the operator typed.
   On `UnauthorizedTopics`, print the topics the server named, state that nothing was applied live, and exit non-zero so a script does not read a rejected apply as success.
   The workspace change written at step 4 is kept; see Q2.

Two states mean "nothing live to update" at step 2: no deployment record (the manager is not running), and a deployment record with no accepted-set file (the running manager predates this version, so every `add` is persist-only until it restarts once).
Both persist and exit zero.
A `put_subscriptions` rejection at step 8 persists and exits non-zero.

`list` prints the persisted workspace list and the recorded accepted set with its timestamp.
It flags ONE direction: topics in the persisted list that are absent from the accepted set, which is what D4's "topics the server silently dropped" means.
The other direction is expected and is never flagged, because `inbox/<session>`, the monitor keys and the lifecycle keys are in the accepted set by design and never in the file: on this team that is 17 topics, so flagging "any difference" would fire on every run of a healthy team and bury the one real signal.
Those 17 print as a separate derived group, labelled as such.
With no workspace file it says the effective list is still the pack's or auto-detected, and with no record it says no live set is recorded, naming which reason applies.
It does not PUT, because a read command must not mutate.

The workspace file and the live set can diverge, and that is detectable rather than hidden.
An `add` issued while the manager is booting can lose the race: the boot PUT is built from the workspace file, so if the CLI's write lands first the topic is live, and if it lands second the file is ahead of the server until the next start.
`list` reports exactly that gap, which is what D4's read-back is for.

## Exact changes per file

### `moda-labs/bobi-agent`

**`bobi/events/subscriptions.py`**
Add `workspace_subscriptions(project_path) -> list[str] | None`, reusing `_normalize_explicit_subscriptions` (`:13`) and the env interpolation `explicit_subscriptions` applies at `:46-48`.
Add `WorkspaceSubscriptionsError(path, cause)`, raised by it on a parse error.
Have `explicit_subscriptions` (`:25`) return the workspace list when it is not `None`.
Have `discover_subscriptions` (`:51`) consult `workspace_subscriptions` before and outside the `try` at `:59-66`, gating on `is not None`.
Leave the pack gate at `:61` unchanged.
No seeder, and no new pack-only reader.

`bobi/service.py` is NOT changed.
`:625-642` stays exactly as it is.

**`bobi/events/state.py`**
Add `accepted_subscriptions_path`, `load_accepted_subscriptions` and `save_accepted_subscriptions`, following `session_cursor_path` (`:37`) for the path shape, including `_safe_session` (`:28-29`), and `atomic_write_json` for the write.
Every write takes `file_lock` on the record.
`save_deployment_state` (`:55-64`) is unchanged.

**`bobi/events/server.py`**
Add a module-level `put_subscriptions(base_url, deployment_id, api_key, subscriptions) -> list[str] | None`.
It is the closure at `bobi/subagent.py:1926-1946` moved out, same contract: `bobi/http.put` (`:78`) with `timeout=10.0`, body `{"replace": [...], "protocol": protocol_payload()}`, then `raise_for_protocol_error`, then an `UnauthorizedTopics` raise on `400 {"error":"unauthorized_topics"}` matching the register path at `:840-842`, then `raise_for_status`, the `"error" in data` guard, and `validate_server_response`.
The register path raises BEFORE `raise_for_protocol_error` and this one after; either order works, because that function only raises for `invalid_protocol`, `incompatible_protocol` and 426, so it passes a `400 unauthorized_topics` body through untouched.
It returns the body's `subscriptions` array, or `None` when the body omits it.
`authorize_resources` (`:680-684`) is unchanged.

**`bobi/subagent.py`**
Delete the `_put_subscriptions` closure at `:1926-1946` and call `events.server.put_subscriptions` instead; it closes over `es_url` and `protocol_payload`, so the CLI cannot reuse it in place.
Record the accepted set at `:1834` (the `authorized` list) and at `:1891` (the helper's return value).
Change `_resubscribe_on_deaf` (`:1974-1982`) to read its own session's record, falling back to `active_subscriptions`.
Extend `_sync_saved_deployment`'s error classifier (`:1894-1924`) with an `UnauthorizedTopics` branch that names the topics, so the new raise does not downgrade the existing boot message from "resource grants rejected (HTTP 400)" to "unexpected subscription failure".
Keep `filter_unauthorized=False` at `:1883`.
`active_subscriptions` (`:1765`, `:1812`, `:1835`, `:1867`, `:1892`, `:1982`) stays as the fallback.

**`bobi/events/client.py`**
Fix the stale "(and re-registers on failure)" clause at `:501-502`.
Raise the swallowed-exception log at `:518-519` from `debug` to `warning`, so a voided repair PUT is visible.

**`bobi/doctor.py`**
`_check_ingress_reachability` gains `except WorkspaceSubscriptionsError` returning `ok=False` with the file path, ahead of the existing `except FileNotFoundError` (`:673-675`) and `except Exception` (`:676-678`) arms, both unchanged.

**`bobi/cli.py`**
Add the `subscriptions` group with `list`, `add`, `remove`, then `main.add_command(subscriptions)`.
Add `"subscriptions"` to the group list at `:4168`, without which `bobi agent <name> subscriptions` does not resolve.
Add it to the pop list at `:4177-4179` so it is not also a top-level command.

**`tests/test_event_subscription.py`**
The three exact-shape assertions on the deployment record (`:71`, `:469-471`, `:514-515`) stay as they are, unchanged, which is already the proof that the new write never touches the credential document.
They do NOT gain an assertion that the accepted-set file exists: both of those tests answer every request with a bare `{"ok": True}` (`:456`, `:499`), so `put_subscriptions` returns `None`, nothing is recorded by the absent-or-empty rule, and the file correctly does not exist.
Changing those mock replies to carry a `subscriptions` array would destroy the property the `{"ok": True}` shape exists to prove, which is that the client tolerates a minimal legacy reply.
The write sites are covered by new test 7 instead.

**Docs**
`docs/EVENT_SERVER.md:378-398`: the resolution order gains a level above "Explicit", and the "on top of that" paragraph at `:396-398` stays correct.
`docs/BUILDING_AGENT_TEAMS.md:129-133`: `subscribe:` stops being the live source of truth once a workspace file exists, and omitting it still means auto-detection.
`skills/bobi.md`: the new CLI group, which `AGENTS.md:12` names as the CLI command reference.
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
The director-maintained tracker bindings, carrying the content removed from `agent.yaml:151-162`.
Seeded by the existing `seed_workspace` (`bobi/install.py:174-192`), which copies pack `workspace/` templates only if absent, so a later install never overwrites director edits.
Four packs already ship a `workspace/` directory through that path (`gtm-team`, `market-research`, `support-manager`, `zachs-personal-assistant`), so the mechanism is proven.

**`agents/moda-eng-team/roles/director/ROLE.md`**
Point `:12` and `:22` at `workspace/managed-repos.yaml` instead of the unnamed "configuration", and say the director keeps that file current.
Resolve the agent name from `BOBI_ROOT` rather than a literal `<name>` in any command the prompt prints: this deployment is installed as `eng-team` while the pack's `agent:` is `moda-eng-team`.

**`agents/baohua/`** (comment-only)
`agent.yaml:105` and `:118`, and `README.md:152`, describe the pack `subscribe:` short-circuit as the top of the resolution order.
Update the prose; baohua's behavior is unchanged, since its pack list is still its seed.

## Test plan

The risk lives in the real event server and in cross-session isolation, so the load-bearing tests are integration tests where the harness can reach them and unit tests on the real seam where it cannot.

**Integration, against the real local event server**

The fixture is `tests/integration/test_event_server.py:371` (`event_server`), with `:394` for a deployment and `:62` for seeded resource grants.
`tests/integration/test_e2e_event_flow.py:87` already boots a real manager against a real event server and publishes a `github:` topic through to it, and `tests/integration/test_event_isolation.py:211` already runs two real session subprocesses against one server.

1. An ungranted global topic in a `replace` returns `400 unauthorized_topics` naming it, and the deployment's prior subscriptions are unchanged.
   This is the no-partial-write behavior the design relies on, and it is NOT covered today: `event-server/test/core.spec.ts` asserts `unauthorized_topics` only for the register path (`:2030`), never for `handleUpdateSubscriptions`.
2. A CLI `add` makes the server route the new topic and a CLI `remove` stops it, with no restart, and the deployment id and api key are unchanged across both, proving identity is preserved and no `register()` happened.
3. After a CLI `add`, the live set still contains every monitor key, both lifecycle keys, and `inbox/<manager session>`: a published `agent/session.completed` is still delivered, and `bobi agent <name> message` still reaches the manager.
   The same test pauses a monitor between boot and the `add`, so it also covers the case where `package/monitors.yaml` changed since the boot PUT.
   This is the regression test for the largest trap.
4. Cross-session isolation survives the change: on the `test_event_isolation.py` harness, a worker's deployment never gains a `github:` topic while the manager's CLI change is applied.

**Unit, on the deaf-reconnect and PUT seam**

The deaf path is entered only by the heartbeat detector after `_HEARTBEAT_TIMEOUT_S = 95.0` seconds of no pong (`bobi/events/client.py:260`, `:294-297`), so these drive `on_deaf_reconnect` off a patched client, exactly as `tests/test_event_subscription.py:110-136` already does, with no 95s wait.

5. A worker session's deaf reconnect PUTs only `inbox/<worker>`, never a `github:`/`slack:`/`linear:` topic, with a MANAGER record also on disk, so it proves the hook reads its own session's record.
   This extends `test_deaf_reconnect_uses_filtered_registered_subscriptions` (`:110-136`), which already asserts the `["inbox/self"]` PUT; the own-record half is the new coverage.
6. A manager's deaf reconnect after a CLI change PUTs the recorded set, not the boot-time list.
7. Both write sites record, and an omitted echo does not erase: a PUT returning `{"subscriptions": [...]}` is recorded, a LATER PUT whose 200 body omits `subscriptions` leaves that record intact, and the next deaf reconnect replays it rather than `{"replace": []}`.
   Two PUTs are required; a single `{"ok": True}` PUT proves nothing, which is why the existing tests at `:456` and `:499` are left alone.
8. A PUT that 400s with `unauthorized_topics` raises `UnauthorizedTopics`, `_sync_saved_deployment`'s classifier names the topics and still reports "resource grants rejected" rather than "unexpected subscription failure", and on the deaf path the same raise is logged at `warning` by `bobi/events/client.py:518-519`.

**Unit, on the subscription surface**

9. With `workspace/subscriptions.yaml` present and `subscribe: []`, `discover_subscriptions` returns `[]`.
   Written against the pre-change code this is red for the right reason: the pre-change gate ignores the file entirely and returns the pack list.
   A version that passes before the change is vacuous and does not count, and that bar applies to every test in this plan.
10. A malformed `workspace/subscriptions.yaml` does NOT produce an auto-detected set.
    Asserted against the real detector rather than a mock, so the test fails if the read is ever moved inside the `try` at `:59-66`.
    The same test asserts `bobi/supervisor/snapshot.py:104-109` still returns an empty expectation list rather than raising, and that `bobi doctor`'s ingress check reports `ok=False` with the path.
11. `explicit_subscriptions` and `discover_subscriptions` agree with a workspace file present, extending the parametrized `tests/test_ingress.py:260-276` so the D078 pin covers the new precedence.
    Its cases include an absent file (reader returns `None`), `subscribe: []` (returns `[]`) and a non-empty list.
12. Two concurrent `add` calls both survive, which is what the re-read inside `file_lock` at CLI step 4 is for.
13. `subscriptions add` on a team with no workspace file seeds it from the effective set, so the previously auto-detected topics are still present afterwards, and the file it writes holds the RESOLVED topics.
14. `subscriptions add` with no manager running, and with a deployment record but no accepted-set file, persists the file, reports that nothing was applied live, exits zero, and never calls `ensure_running`.
15. `subscriptions add` with an empty bubble state fails with that reason and does not PUT.

Four assertions are deliberately NOT new tests, each already covered.
`replace` returning the accepted set: `event-server/test/core.spec.ts:2284-2306` (`added: 1`, `removed: 1`, the exact `subscriptions` array) and over real HTTP at `tests/integration/test_event_server.py:510-520`.
`{"replace": []}` returning 400: `core.spec.ts:2314-2324`.
A `remove` whose survivors were granted at an earlier boot: `core.spec.ts:2284-2306` seeds the grant before the replace and the replace passes, and CLI step 6 has `remove` skip authorization by construction.
`bobi/service.py` is not changed at all, so `tests/test_service.py:21-26` needs no attention.

## Migration

No behavior change on upgrade, for any pack shape, because nothing writes a workspace file until an operator runs `add` or `remove`.

Until then every pack resolves its subscriptions exactly as it does today: a pack list short-circuits, and a pack without one auto-detects.
The first boot after the upgrade composes the same list as the last boot before it, PUTs it, and the server's `added: 0, removed: 0` echo becomes the first recorded accepted set.
Test 14 covers the window before that boot, when the CLI persists but cannot apply.

The deployment id, api key, and event cursor are untouched: nothing in this change calls `register()`, nothing unlinks a cursor, and the credential record is not rewritten.

### Release ordering

The fleet's bobi version is pinned in `moda-labs/moda-agents` `.github/fleet-version` and moved automatically by `version-gate.yml:100` once `ci-canary` proves a release, so the pin's current value is not worth stating here.

Order:

1. Land and release the bobi-agent change; let the canary prove it and the pin PR land.
2. Land the moda-agents companion PR.
3. Roll the fleet.

Step 2 must not precede step 1: the director prompt would otherwise name commands a fleet on the pre-change pin does not have.

## Out of scope

- A GET route for live subscriptions.
  D4 asks for one "only if needed", and it is not: the PUT response already returns the accepted set (`core.ts:1537-1540`, proved at `core.spec.ts:2300-2301` and over real HTTP at `tests/integration/test_event_server.py:517-520`), and that set is now recorded.
- Making a `whatsapp:`, `discord:` or new-workspace `slack:` topic apply LIVE from the CLI.
  Their grants are written by the channel registrations the manager runs at session start (`bobi/subagent.py:1767`), which the CLI does not run, so such a topic persists to the workspace file and applies at the next start.
  An in-workspace `slack:` channel does apply live, because the grant is keyed on the team id.
- Hot-reloading anything else from the workspace.
  This change applies exactly one key.

## Open questions

Revision 4 closes the previous Q1, the read-back mechanism.
D4 says "Prefer reusing the PUT response if it already returns the accepted set; add a GET route only if needed", and the response does return it (`core.spec.ts:2300-2301`, and over real HTTP at `tests/integration/test_event_server.py:517-520`), so D4's own condition decides it.
`list` shows the recorded set labelled "last accepted at T"; no GET route, and no read-command PUT, which `core.ts:1531-1536` would turn into a write.

**Q1. Should `workspace/subscriptions.yaml` exist before the first `add`?**
As specified it does not: the CLI creates it, and until then a pack list short-circuits and a pack without one auto-detects, exactly as today.
That is the smaller design, it is a strictly smaller migration, and a boot-time seeder would need a gate that can tell an absent `subscribe:` from `subscribe: []` (the reader cannot) to avoid writing an authoritative empty list for the auto-detecting majority of the fleet.
The cost is discoverability: a director that wants to read its own subscription list has to run `subscriptions list` rather than `cat` the file.
This is also the one place where the design takes a reading of D2 ("Subscriptions live as a list in the WORKSPACE; the pack `agent.yaml` `subscribe:` is only the SEED"): the list lives in the workspace from the first mutation onward, not from the first boot.
My recommendation is to keep it lazy.
If you want the file present from the first boot, say so and the seeder plus its gate come back, at about 40 lines and three more tests.

**Q2. When the server rejects the live apply, should `add` keep the workspace change?**
With `filter_unauthorized=False`, an added topic with no grant is kept in the PUT and the server 400s the whole update, so nothing is applied live and the workspace file is ahead of the server.
Keeping it treats the workspace list as the operator's declared intent and lets a later grant (usually the GitHub App install, moments later) make it live at the next boot or the next `add`.
Rolling it back refuses to persist something that does not work today, at the cost of making the correct onboarding order fragile.
My recommendation is to keep it, report the server's exact `unauthorized_topics` list, and exit non-zero so a script does not read the failure as success.
