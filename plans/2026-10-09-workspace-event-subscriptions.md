# Workspace-owned event subscriptions

Issue: [#952](https://github.com/moda-labs/bobi-agent/issues/952)
Status: spec, awaiting approval (Gate 1).
Written 2026-10-09 from scratch, replacing the generic-overlay design in PR #956.

Every `bobi/`, `event-server/`, `tests/` and `docs/` file:line below was read at `83bebe49` (`origin/main`, 2026-10-09).
Every `moda-agents` file:line was read at `bf9d1088` (`origin/main`, 2026-10-08).
Line numbers from the August review rounds have moved and are not reused.

Revision 2 after an independent review round (`plans/reviews/2026-10-09-952-subscriptions-review-1.md`).
That round found three blockers in revision 1, and the design below is smaller as a result: there is no composition function and no change to `bobi/service.py`.

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
It persists the workspace list and hot-applies via the existing identity-preserving `PUT /deployments/<id>/subscriptions {"replace":[...]}`; no restart needed.

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
`docs/EVENT_SERVER.md:396-398` states the same composition, including "the session's own `inbox/<self>`".

So the live set is `["inbox/<session>"] + composed`, and `{"replace": [...]}` is authoritative: `event-server/core/src/core.ts:1531-1536` assigns `deployment.subscriptions = desired` and removes every stored subscription not in `desired`.

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

### The per-session deployment record is the right home for the accepted set

`bobi/events/state.py:32-66` already keeps one JSON file per session at `<run>/state/deployments/<session>.json`, holding `deployment_id` and `api_key`.
It is written at `bobi/subagent.py:1834` and read at `:1761` and `bobi/service.py:384`.
Both PUT sites already compute the exact set they asserted and assign it to `active_subscriptions` on the next line (`bobi/subagent.py:1835`, `:1892`), so persisting it there costs one argument.

`event-server/core/src/core.ts:1537-1540` returns `{subscriptions, added, removed, protocol}` on a `replace`, and `bobi/subagent.py:1938-1946` reads that body, validates it, and throws it away.
The data D4 asks for is already on the wire and already at the two places that need to record it.

### The empty-list fallthrough is a real hazard, and the malformed case is worse

`bobi/events/subscriptions.py:61` gates on `if explicit:`.
An empty list is falsy, so control falls through to `:68-78`, which calls `Config.load` and then `detect()`, and finally returns `[project_path.name]`.
An operator-emptied list does not mean "subscribe to nothing"; it means "auto-detect from git remotes".

Worse, the call at `:60` sits inside a `try` whose `except Exception: pass` at `:63-66` is deliberate and test-pinned by `tests/test_ingress.py:306-324`.
A malformed file read inside that `try` is swallowed and routes to the same auto-detection.

Measured on this deployment at `83bebe49`:

```
$ PYTHONPATH=<main worktree> python3 -c "import bobi.events.adapters as a; from pathlib import Path; \
    print(a._detect_github(Path('/data/.bobi/agents/eng-team/run'), None))"
['github:moda-labs/bobi-agent']
```

So the fallthrough collapses 4 GitHub topics to 1.
`bobi/events/adapters.py:139-142` names the Slack hazard in the same path ("swallowing it here would silently promote a channel-scoped subscription to a workspace-wide one"), and `_detect_linear` (`:290-315`) subscribes to every Linear team the API key can see rather than MDS and MOD.
The pack `subscribe:` is tolerable behind this gate because `<run>/package/` is read-only (`bobi/runtime_guard.py:143-153`).
The new file is not: `<run>/workspace/` is writable, is edited by the CLI by design, and is advertised to the agent brain as its own scratch space (`bobi/prompts/resolver.py:86-97`).

A malformed workspace file must therefore fail loud, not fall through.

### The ingress warning reads a different function

`bobi/ingress.py:83` reads `explicit_subscriptions`, not `discover_subscriptions`.
`tests/test_ingress.py:260-276` pins the two to one parser, and the `explicit_subscriptions` docstring at `:28` names itself "The ONE parser for the `subscribe:` surface (D078)".

A workspace layer added only to `discover_subscriptions` would silently break that invariant while the test stayed green, because the test writes no workspace file.
The workspace layer goes inside `explicit_subscriptions`.

### Authorization is a network write, and it fails open

`authorize_resources` (`bobi/events/server.py:680-684`) is not a filter.
It POSTs the real GitHub/Linear credential to `/resources/authorize` once per global topic (`:650-655`, `:781-784`), needs the signed bubble identity from `<run>/state/bubble.json`, and returns the list UNFILTERED when that identity is absent (`:713-714`: "can't sign - leave the set unchanged").

Its `filter_unauthorized=False` mode is the deliberate policy for an existing deployment, and both the function docstring (`:707-712`) and `_sync_saved_deployment` (`bobi/subagent.py:1859-1864`) give the same reason: "replacing the deployment's subscriptions with a filtered list would silently unsubscribe a valid existing deployment."
The CLI PUTs to that same deployment, so it uses the same mode.
`filter_unauthorized=True` there would drop a topic whose credential has since rotated out of the container env while the server still holds a no-expiry grant, removing it from the live index as a side effect of an unrelated `add`.

### The server rejects; it does not silently drop

`core.ts:1508-1512` calls `unauthorizedGlobalTopics` and, on any ungranted topic, returns `400 {"error":"unauthorized_topics","topics":[...]}`, rejecting the whole update with no partial write.
`core.ts:1498-1500` returns `400 replace[] must not be empty`, so removing the last explicit topic cannot be expressed as an empty replace (in practice the live set always contains at least `inbox/<session>`).

The silent drop is client-side, in `authorize_resources` with `filter_unauthorized=True`, which only the register path uses (`bobi/subagent.py:1805-1808`).
So D4's "topics the server silently dropped" is two different things, and with `filter_unauthorized=False` on the new path only the first applies: a 400 naming the exact topics.
Neither needs a GET route.

### Two stale facts in the current code

`bobi/events/client.py:501-502` claims the deaf-reconnect hook "re-registers on failure"; it does not.
Only `_register_with_retry` unlinks the cursor and re-registers (`bobi/subagent.py:1838`); `_sync_saved_deployment`'s PUT error path (`:1894-1924`) explicitly retains both and says so at `:1917-1920` ("saved deployment and completion cursor retained; retry pending").

`bobi/events/client.py:518-519` catches every non-protocol exception from the hook and logs it at `log.debug`.
Combined with the all-or-nothing 400, one topic that lost its grant voids the entire repair PUT, invisibly in normal operation.

### `managed_repos` has exactly one consumer

`grep -rn "managed_repos" bobi/` exits 1.
The only bobi-agent hits are `tests/test_memory.py:52,55,62,66`, where the string is arbitrary memory-index test data and not the pack key.

Fleet-wide there is one real consumer: `moda-labs/moda-agents` `scripts/check-deploy-compose.py:85-94` parses `managed_repos` off the composed pack `agent.yaml` and fails the deploy-compose check unless `moda-labs/moda-skills` maps to `github-issues`.
The director ROLE prompt is not a consumer today: `agents/moda-eng-team/roles/director/ROLE.md:12` and `:22` say "from configuration" and "in configuration" and never name the key, so `agent.yaml:155` ("Advisory tracker binding read by the director prompt") is already false.
D5's prompt half is an addition, not an update.

## Design

One new workspace file, one new CLI group, one new field on an existing state record.
No composition function, no change to `bobi/service.py`, no new server route, no overlay, no merge semantics, no new error class.

### The file

`<run>/workspace/subscriptions.yaml`, a mapping with one key:

```yaml
# Event topics this team subscribes to.
# Seeded from the agent pack on first manager boot; the pack is only the seed.
# Edit with `bobi agent <name> subscriptions add|remove`, which applies the
# change to the running event server without a restart.
subscribe:
  - github:moda-labs/lightweave
  - github:moda-labs/moda-agents
  - github:moda-labs/bobi-agent
  - github:moda-labs/moda-skills
  - slack:T0952RZRZ0X:app:A0BDLA833MW:C0BAEN48KQR
  - linear:MDS
  - linear:MOD
```

That body is the seed this team gets, taken from `agents/moda-eng-team/agent.yaml:128-134` at `bf9d1088`.

A mapping under the same `subscribe:` key, rather than a bare YAML list, so the existing parser is reused rather than forked.
`explicit_subscriptions` already requires a dict and reads `raw.get("subscribe", [])` (`bobi/events/subscriptions.py:43-48`).
Keeping one parser for one wire shape is less new code and preserves the D078 invariant its docstring claims at `:28`.

Monitor topics, lifecycle topics and `inbox/<session>` are NOT in this file.
They are derived from `monitors:`, from the framework, and from the session's own identity, so writing them here would create a second source of truth that drifts.

The CLI writes a fixed header constant plus `yaml.safe_dump({"subscribe": [...]})`.
Only PyYAML is available (`pyproject.toml` declares `pyyaml>=6.0` and nothing else), and `safe_dump` discards comments, so a comment the operator or the agent adds below the header does not survive an `add`.
That is stated in the header itself rather than worked around with line splicing.

### Presence, not truthiness

`bobi/events/subscriptions.py` gains one reader:

```
workspace_subscriptions(project_path) -> list[str] | None
```

`None` means the file is absent.
`[]` means the operator subscribes to nothing explicit, and is returned as such.
A malformed file raises, and the exception carries the file path.

`explicit_subscriptions` consults it first and returns it when it is not `None`, so the one parser keeps serving both its callers and the ingress warning at `bobi/ingress.py:83` follows the workspace layer for free.

`discover_subscriptions` calls `workspace_subscriptions` directly, BEFORE and OUTSIDE the `try` at `:59-66`, and returns the list when it is not `None`.
That placement is the whole point.
Inside the `try`, a malformed workspace file would be swallowed into auto-detection; outside it, the manager boot fails with the path, while a malformed PACK `agent.yaml` keeps its historical fall-through, which `tests/test_ingress.py:306-324` pins.

The pack's own truthiness gate at `:61` is left alone, deliberately.
Nothing writes `package/agent.yaml` at runtime, so an empty pack list is not a reachable operator mistake, and `tests/test_symmetric_node.py:90-102` pins the current behavior.
(The `monitors` commands DO write `package/monitors.yaml` under `with_mutable_runtime_package`; `agent.yaml` has no such path.)

The two other readers already degrade safely.
`bobi/supervisor/snapshot.py:105-108` wraps its `discover_subscriptions` call in `except Exception: pass` and is documented as best-effort, so a malformed file yields no declared subscriptions and silence detection simply asserts less.
`bobi/service.py:183-194` wraps `check_ingress_reachability` the same way, so the reachability warning is skipped at `log.debug`.
Neither seeds, and neither is changed.

### Seeding

One call at the start of the manager boot path in `bobi/service.py`, before the read at `:625`: if `<run>/workspace/subscriptions.yaml` is absent, write it with the installed pack's `subscribe:` value.

The manager is not the only writer (the CLI writes too), so every writer goes through one locked helper.
`bobi/fsutil.py:162` `file_lock` is held across load, mutate and save; `AGENTS.md:97` requires it for read-modify-write state, and `atomic_write_text` alone keeps the file parseable without stopping a concurrent updater's change from being overwritten.
Seeding takes the same lock, so an `add` racing a cold start cannot be lost.

Seeding at boot rather than in `seed_workspace` (`bobi/install.py:174-192`) is deliberate.
`seed_workspace` copies pack `workspace/` templates and knows nothing about `subscribe:`; teaching it a specific key would break the "general purpose" ruling, and the eng-team pack has no `workspace/` directory to copy from today.

### The accepted set, recorded per session

`save_deployment_state` (`bobi/events/state.py:55-66`) gains two optional fields on the per-session record: `subscriptions`, the set the server confirmed, and `subscriptions_at`, an ISO timestamp.

`put_subscriptions` returns the `subscriptions` array from the response body (`core.ts:1539`), which the current closure discards at `bobi/subagent.py:1938-1946`.
Both existing PUT/register sites record it on the line where they already assign `active_subscriptions`: `bobi/subagent.py:1834-1835` (register) and `:1891-1892` (sync).

This record is what makes the rest of the design small.
It is the live truth for exactly one session, written only by that session or by a CLI command targeting it, and it already lives beside the credentials and the cursor that session needs.

`_resubscribe_on_deaf` (`bobi/subagent.py:1974-1982`) reads its OWN session's record and PUTs that, falling back to `active_subscriptions` when the record has no `subscriptions` field.
A worker therefore replays `["inbox/<worker>"]`, and the manager replays whatever was last accepted, including a change the CLI made minutes ago.
That fixes the revert without a branch, without recomposition, and without any cross-session leak.

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
2. Under `file_lock`, load `workspace/subscriptions.yaml`, apply the topic edit, and write it back with `atomic_write_text` (`bobi/fsutil.py:100`).
3. Resolve the manager's session: `manager_session_name(project_path)` (`bobi/service.py:135-144`), then `load_deployment_state(project_path, session)` (`bobi/events/state.py:44-52`).
4. Build the PUT list by applying the SAME topic edit to the record's `subscriptions`.
   Nothing is recomposed, so monitor, lifecycle and `inbox/` topics pass through untouched by construction.
5. For `add` only, call `authorize_resources(..., filter_unauthorized=False)` so a newly added global topic has its grant written before the PUT.
   `remove` skips it: removing a topic needs no grant, and calling it would re-POST credentials for every remaining topic.
   If `load_bubble_state` (`bobi/events/state.py:79`) is empty the command FAILS with that fact, rather than proceeding into the unfiltered pass-through at `bobi/events/server.py:713-714`.
6. Resolve `es_url` from `cfg.event_server_url` with the `http://localhost:8080` fallback and `ensure_running` when `local_port_from_url` matches, exactly as `bobi/subagent.py:1947-1955` does.
7. `PUT /deployments/<id>/subscriptions` with `{"replace": [...], "protocol": protocol_payload()}`.
   The `protocol` key is optional on the wire (`core.ts:37` returns early when it is absent, treating that as legacy v1), and is sent to match the existing PUT at `bobi/subagent.py:1931`.
8. Record the response's `subscriptions` and the timestamp on the manager's session record.
9. Print the full diff of the previous accepted set against the new one, naming every add and every remove, not just the topic the operator typed.
   On a `400 unauthorized_topics`, print the topics the server named and state that nothing was applied live.

If no deployment record exists the manager is not running.
If the record exists but has no `subscriptions` field, the running manager predates this version and there is no accepted set to edit.
In both cases the CLI still persists the workspace change, says plainly that nothing was applied live and that the change takes effect at next start, and exits zero: persisting succeeded, and that is the whole operation when there is nothing live to update.

`list` prints the persisted workspace list, the recorded accepted set with its timestamp, and flags any difference.
It does not PUT, because a read command must not mutate.
See Q1.

The workspace file and the live set can diverge, and that is detectable rather than hidden.
An `add` issued while the manager is booting can lose the race: the boot PUT is built from the workspace file, so if the CLI's write lands first the topic is live, and if it lands second the file is ahead of the server until the next start.
`list` reports exactly that gap, which is what D4's read-back is for.

## Exact changes per file

### `moda-labs/bobi-agent`

**`bobi/events/subscriptions.py`**
Add `workspace_subscriptions(project_path) -> list[str] | None`, reusing `_normalize_explicit_subscriptions` (`:13`) and the env interpolation `explicit_subscriptions` applies at `:46-48`; a parse error raises with the path.
Add `seed_workspace_subscriptions(project_path) -> None`, write-if-absent under `file_lock`.
Have `explicit_subscriptions` (`:25`) return the workspace list when it is not `None`.
Have `discover_subscriptions` (`:51`) consult `workspace_subscriptions` before and outside the `try` at `:59-66`, gating on `is not None`.
Leave the pack gate at `:61` unchanged.

**`bobi/service.py`**
Call `seed_workspace_subscriptions(project_path)` before the read at `:625`.
Nothing else changes; `:625-642` stays as it is.

**`bobi/events/state.py`**
Extend `save_deployment_state` (`:55-66`) with optional `subscriptions` and `subscriptions_at`, preserving the existing two fields and the atomic write.

**`bobi/events/server.py`**
Add a module-level `put_subscriptions(base_url, deployment_id, api_key, subscriptions) -> list[str]` that PUTs and returns the server's reported `subscriptions`.
`authorize_resources` (`:680-684`) is unchanged.

**`bobi/subagent.py`**
Delete the `_put_subscriptions` closure at `:1926-1946` and call `events.server.put_subscriptions` instead; it is a closure over `es_url` and `protocol_payload`, so the CLI cannot reuse it in place.
Record the returned set via `save_deployment_state` at `:1834` (register) and after the PUT at `:1891`.
Change `_resubscribe_on_deaf` (`:1974-1982`) to read its own session's recorded set, falling back to `active_subscriptions`.
Keep `_sync_saved_deployment`'s `filter_unauthorized=False` at `:1883`.
`active_subscriptions` (`:1765`, `:1812`, `:1835`, `:1867`, `:1892`, `:1982`) stays as the fallback.

**`bobi/events/client.py`**
Fix the stale "(and re-registers on failure)" clause at `:501-502`.
Raise the swallowed-exception log at `:518-519` from `debug` to `warning`, so a voided repair PUT is visible.

**`bobi/cli.py`**
Add the `subscriptions` group with `list`, `add`, `remove`, then `main.add_command(subscriptions)`.
Add `"subscriptions"` to the group list at `:4168`, without which `bobi agent <name> subscriptions` does not resolve.
Add it to the pop list at `:4177-4179` so it is not also a top-level command.

**Docs**
`docs/EVENT_SERVER.md:378-398`: the resolution order gains a level above "Explicit", and the "on top of that" paragraph at `:396-398` stays correct but now describes a list seeded from the pack.
`docs/BUILDING_AGENT_TEAMS.md:129-133`: `subscribe:` stops being the live source of truth and becomes the seed.
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

The risk lives in the real event server and in cross-session isolation, so the load-bearing tests are integration tests rather than mocks.
The local event-server fixture is `tests/integration/test_event_server.py:371` (`event_server`), with `:394` for a deployment and `:62` for seeded resource grants.

**Integration, against the real local event server**

1. An ungranted global topic in a `replace` returns `400 unauthorized_topics` naming it, and the deployment's prior subscriptions are unchanged.
   This is the no-partial-write behavior the design relies on, and it is NOT covered today: `event-server/test/core.spec.ts` asserts `unauthorized_topics` only for the register path (`:2030`), never for `handleUpdateSubscriptions`.
2. A CLI `add` on a running manager changes what the server routes: publish an event on the newly added topic and assert the manager's session receives it, with no restart.
3. A CLI `remove` stops delivery of that topic's events, and the deployment id and api key are unchanged across both, proving identity is preserved and no `register()` happened.
4. After a CLI `add`, the live set still contains every monitor key, both lifecycle keys, and `inbox/<manager session>`: a published `agent/session.completed` is still delivered, and `bobi agent <name> message` still reaches the manager.
   This is the regression test for the largest trap.
5. A CLI `add` does not change monitor delivery when `monitors pause` ran since the last boot: pause a monitor, `add` an unrelated topic, and assert the paused monitor's topic is still in the live set.
6. Worker isolation across the deaf path: start an inbox-only worker and the manager, force the worker's deaf reconnect, and assert the worker's PUT carries only `inbox/<worker>` and that its deployment never gains a `github:` topic.
7. Manager deaf reconnect after a CLI change: run `subscriptions add`, force the manager's deaf path, and assert the PUT carries the new topic rather than the boot-time list.

**Unit and falsifiable**

8. With `workspace/subscriptions.yaml` present and `subscribe: []`, `discover_subscriptions` returns `[]`.
   Written against the pre-change code this is red for the right reason once the reader exists: the pre-change gate ignores the file entirely and returns the pack list.
   A version that passes before the gate changes is vacuous and does not count.
9. A malformed `workspace/subscriptions.yaml` does NOT produce an auto-detected set.
   Asserted against the real detector rather than a mock, so the test fails if the read is ever moved inside the `try` at `:59-66`.
   The same test asserts `bobi/supervisor/snapshot.py:105-108` still returns an empty expectation list rather than raising.
10. `explicit_subscriptions` and `discover_subscriptions` agree with a workspace file present, extending `tests/test_ingress.py:260-276` so the D078 pin actually covers the new precedence.
11. `workspace_subscriptions` returns `None` for an absent file and `[]` for `subscribe: []`.
12. Seeding is write-if-absent: a second boot does not overwrite an edited file, and a boot with the file present does not touch it.
13. Two concurrent `add` calls both survive, and an `add` racing boot seeding is not lost.
14. On an upgrade of an existing deployment with no workspace file, the first boot's composed list is byte-identical to the pre-upgrade composed list.
15. `subscriptions add` with no manager running, and with a record that has no `subscriptions` field, persists the file, reports that nothing was applied live, and exits zero.
16. `subscriptions add` with an empty bubble state fails with that reason and does not PUT.

Two assertions are deliberately NOT new tests.
`replace` returning the accepted set is already covered at `event-server/test/core.spec.ts:2284-2306` (`added: 1`, `removed: 1`, the exact `subscriptions` array) and over real HTTP at `tests/integration/test_event_server.py:510-520`.
`{"replace": []}` returning 400 is already covered at `core.spec.ts:2314-2324`.

The composition lift is gone, so there is nothing to keep faithful; `tests/test_service.py:21-26`, which monkeypatches `subscriptions.discover_subscriptions`, `monitor_subscription_keys`, `lifecycle_subscription_keys` and `MonitorRegistry.load` as module attributes, keeps working untouched.

## Migration

No behavior change on upgrade.

An existing deployment has no `workspace/subscriptions.yaml`.
The first manager boot after the upgrade seeds it from the installed pack's `subscribe:`, so the composed list is identical to what the same boot would have produced before.
`_sync_saved_deployment` then PUTs that identical set, the server reports `added: 0, removed: 0`, and that response becomes the first recorded accepted set.
Test 14 asserts the first half; test 15 covers the window before that boot, when the CLI persists but cannot apply.

The deployment id, api key, and event cursor are untouched: nothing in this change calls `register()` and nothing unlinks a cursor.

### Release ordering

`moda-labs/moda-agents` pins the fleet's bobi version in `.github/fleet-version`, read by `deploy-agent-teams.yml:91` and `:177` and by `team-images.yml:139`, and rewritten by `version-gate.yml:100` after `ci-canary` proves a version.
(`release-fleet.yml:342` installs `inputs.version`, the candidate under test, not the pin.)
At `bf9d1088` the pin reads `BOBI_VERSION=0.59.0`; re-read it at approval time, since `version-gate.yml` moves it automatically.

Order:

1. Land and release the bobi-agent change; let the canary prove it and the pin PR land.
2. Land the moda-agents companion PR.
3. Roll the fleet.

Step 2 must not precede step 1.
The new `workspace/managed-repos.yaml` seed relies on `seed_workspace`, which already ships, so D5's pack half has no hard dependency on the new CLI.
But the director prompt would name commands that do not exist yet on a fleet pinned to the pre-change version, so ordering it after the pin bump keeps the prompt honest.

## Out of scope

- Any repo concept under `bobi/`. No `repos` CLI, no `managed_repos` awareness in framework code (D3, and Zach's August ruling).
- A GET route for live subscriptions. The PUT response already carries the accepted set and is now recorded. See Q1.
- Changing `_sync_saved_deployment`'s `filter_unauthorized=False` (`bobi/subagent.py:1883`). The CLI now matches it rather than diverging from it.
- Making the pack layer presence-gated rather than truthiness-gated. Argued above under "Presence, not truthiness".
- Branch-delete safety machinery. Policy and prompt, per D5's accepted trade-off.
- Hot-reloading anything else from the workspace. This change applies exactly one key.
- A pure grant-check that does not write. `authorize_resources` always attempts authorization; adding a read-only variant is a separate change, and `filter_unauthorized=False` makes the server authoritative, which is enough here.

## Open questions

**Q1. Is a recorded accepted set good enough for `subscriptions list`, or should it get a GET route?**
Three options, and D4 prefers the first.
(a) Record what the last PUT accepted, and have `list` show "last accepted, as of T" beside the persisted list.
It adds no server surface, the record is load-bearing anyway (the deaf hook reads it), and it labels its own staleness.
(b) Add a GET route, so `list` is authoritative on demand.
(c) Have `list` issue an idempotent `{"replace": <current accepted set>}`, which returns live truth with `added: 0, removed: 0`.
That needs no new route either, and `tests/integration/test_event_server.py:510-520` proves the shape, but `core.ts:1531-1536` always calls `addSubscription` and `putDeployment`, so a read command would write.
My recommendation is (a): it satisfies D4 as written, and (c) trades a real invariant for freshness between mutations that nothing else depends on.

**Q2. When the server rejects the live apply, should `add` keep the workspace change?**
With `filter_unauthorized=False`, an added topic with no grant is kept in the PUT and the server 400s the whole update, so nothing is applied live and the workspace file is ahead of the server.
Keeping it treats the workspace list as the operator's declared intent and lets a later grant (usually the GitHub App install, moments later) make it live at the next boot or the next `add`.
Rolling it back refuses to persist something that does not work today, at the cost of making the correct onboarding order fragile.
My recommendation is to keep it, report the server's exact `unauthorized_topics` list, and exit non-zero so a script does not read the failure as success.
