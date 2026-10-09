# Workspace-owned event subscriptions

Issue: [#952](https://github.com/moda-labs/bobi-agent/issues/952)
Status: spec, awaiting approval (Gate 1).
Written 2026-10-09 from scratch, replacing the generic-overlay design in PR #956.

Every file:line below was read at `83bebe49` (`origin/main`, 2026-10-09) in a worktree cut from that commit.
Line numbers from the August review rounds have moved and are not reused.

## Problem

Changing which repos a running team hears about means editing agent pack source in a second repository, rebuilding the pack, reinstalling it, and restarting the manager.

The topics live in `subscribe:` in `moda-labs/moda-agents` at `agents/moda-eng-team/agent.yaml:125-132`, installed as a frozen read-only image at `<run>/package/agent.yaml`.
The 2026-08-03 familystories-ai offboard is the measured case: a commit in another repo, a pack rebuild, an install, and a restart.

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

## What I verified, and where the August hazards actually are now

The August rounds left six hazards to re-check.
Three confirmed with moved line numbers, one is now stale, and the re-check turned up five more that change the design.

### Confirmed

**C1. The truthiness gate is real, at a new line.**
`bobi/events/subscriptions.py:61` gates on `if explicit:`.
An empty list is falsy, so control falls through to `:68-78`, which calls `Config.load` and then `detect()`, and finally returns `[project_path.name]`.
An operator-emptied list therefore does not mean "subscribe to nothing"; it means "auto-detect from git remotes".
The workspace list must gate on presence.

**C2. `register()` is the wrong primitive.**
`bobi/events/server.py:849-875` returns a fresh `(deployment_id, api_key)`.
It mints a new deployment, so it cannot be used to change a live one's topics.

**C3. The deaf-reconnect replay would revert an external change.**
`active_subscriptions` is set at `bobi/subagent.py:1765` and reassigned at `:1835` and `:1892`.
`_resubscribe_on_deaf` (`:1974-1982`) PUTs that in-process list whenever the client force-reconnects a deaf path.
A CLI change applied to the server would be overwritten by the next deaf reconnect.

**C4. `managed_repos` has zero consumers under `bobi/`.**
`grep -rn "managed_repos" bobi/` exits 1.
The only hits in the repo are `tests/test_memory.py:52,55,62,66`, where the string is arbitrary memory-index test data and not the pack key.

### Stale

**S1. The PUT error path no longer unlinks the cursor.**
The August brief says both PUT error paths unlink the event cursor and re-register.
On current main only the register path does: `_register_with_retry` unlinks at `bobi/subagent.py:1838`.
`_sync_saved_deployment`'s PUT error path (`:1893-1924`) explicitly retains both, and says so in the operator message it raises at `:1917-1920`: "saved deployment and completion cursor retained; retry pending".
The "history replay into auto-dispatch" failure mode is no longer reachable through the PUT path.
C2 still stands on its own: the CLI must not call `register()`.
The client docstring at `bobi/events/client.py:501-502` still claims the deaf-reconnect hook "re-registers on failure"; it does not, and that line is stale.

### New, and load-bearing

**N1. The PUT already returns the accepted set, so D4 needs no new route.**
`event-server/core/src/core.ts:1536-1540` returns `{subscriptions, added, removed, protocol}` on a `replace`.
`bobi/subagent.py:1938-1946` reads that body, validates it, and throws it away.
The data D4 asks for is already on the wire.

**N2. The server does not silently drop topics; the client does.**
`core.ts:1508-1512` calls `unauthorizedGlobalTopics` and, on any ungranted topic, returns `400 {"error":"unauthorized_topics","topics":[...]}` and rejects the whole update with no partial write.
The silent drop is client-side, in `authorize_resources` with `filter_unauthorized=True` (the default, `bobi/events/server.py:682`), which the register path uses via `bobi/subagent.py:1805-1808`.
The saved-deployment path passes `False` at `:1883` and keeps unverified topics deliberately.
So D4's "topics the server silently dropped" is really two different things: a 400 naming the exact topics, and a client-side filter that drops before the request is sent.
Both are reportable; neither needs a GET.

**N3. `{"replace": []}` is rejected.**
`core.ts:1498-1500` returns `400 replace[] must not be empty`.
Removing the last explicit topic cannot be expressed as an empty replace.
In practice the composed list is never empty (N4), but an implementation that PUTs the bare workspace list would hit this.

**N4. The live subscription set is NOT the workspace list.**
`bobi/service.py:625` reads the explicit list, then composes:
`:626` appends `extra_subscribe`, `:634-636` appends `monitor_subscription_keys(...)`, `:640-642` appends `lifecycle_subscription_keys()`.
That composed list is what reaches the session at `:759`.
A CLI that PUTs only the persisted workspace list would silently unsubscribe the manager from monitor findings and from sub-agent `session.completed`/`session.failed` delivery.
This is the single largest implementation trap in the change, and it is why composition must be extracted rather than re-derived.

**N5. A deaf-reconnect PUT failure is swallowed at debug level.**
`bobi/events/client.py:507-519`: a protocol error stops the client, and every other exception is caught at `:518` and logged with `log.debug`.
Combined with N2's all-or-nothing 400, one topic that lost its grant would void the entire repair PUT, invisibly in normal operation.
So the deaf-reconnect path must filter client-side before PUTting, not pass a raw list.

**N6. `managed_repos` is not consumer-free fleet-wide.**
`moda-labs/moda-agents` `scripts/check-deploy-compose.py:85-94` parses `managed_repos` off the composed pack `agent.yaml` and fails the deploy-compose check unless `moda-labs/moda-skills` maps to `github-issues`.
C4 is about `bobi/` and holds there.
D5's pack removal breaks that check unless the same commit updates it.

## Design

One new workspace file, one new CLI group, one composition function shared by boot and CLI.
No new server route, no overlay, no merge semantics, no new error class.

### The file

`<run>/workspace/subscriptions.yaml`, a mapping with one key:

```yaml
# Event topics this team subscribes to.
# Seeded from the agent pack on first manager boot; the pack is only the seed.
# Edit with `bobi agent <name> subscriptions add|remove`, which applies the
# change to the running event server without a restart.
subscribe:
  - github:underminedsk/lightweave
  - github:moda-labs/moda-agents
  - github:moda-labs/bobi-agent
  - github:moda-labs/moda-skills
  - slack:T0952RZRZ0X:app:A0BDLA833MW:C0BAEN48KQR
  - linear:MDS
  - linear:MOD
```

A mapping under the same `subscribe:` key, rather than a bare YAML list, so the existing parser is reused rather than forked.
`explicit_subscriptions` already requires a dict and reads `raw.get("subscribe", [])` (`bobi/events/subscriptions.py:43-48`), and its docstring names itself "The ONE parser for the `subscribe:` surface (D078)".
Keeping one parser for one wire shape is less new code and preserves that invariant.

Monitor and lifecycle topics are NOT in this file.
They are derived from `monitors:` and from the framework itself (N4), so writing them here would create a second source of truth that drifts.

### Presence, not truthiness

`subscriptions.py` gains one function:

```
workspace_subscriptions(project_path) -> list[str] | None
```

`None` means the file is absent.
`[]` means the operator subscribes to nothing explicit, and is returned as such.

`discover_subscriptions` gates on `is not None`, so an emptied list stops at the workspace layer and never reaches the `Config.load` plus `detect()` fallthrough at `:68-78`.

The pack's own truthiness gate at `:61` is left alone, deliberately.
The pack is a frozen read-only image that no operator edits at runtime, so an empty pack list is not a reachable operator mistake.
Changing both would be a larger diff for no closed failure mode, and the fallthrough still serves a pack with no `subscribe:` at all.

### Seeding

One call at the start of the manager boot path in `bobi/service.py`, before the read at `:625`:
if `<run>/workspace/subscriptions.yaml` is absent, write it with the installed pack's `subscribe:` value, then read it back.

The manager is the only writer.
The supervisor heartbeat (`bobi/supervisor/snapshot.py:107`) and the ingress reachability check (`bobi/ingress.py:83`) stay pure readers and never seed.
That keeps a single writer for a file two other processes read.

Seeding at boot rather than in `seed_workspace` (`bobi/install.py:174-192`) is deliberate.
`seed_workspace` copies pack `workspace/` templates and knows nothing about `subscribe:`; teaching it a specific key would break the "general purpose" ruling, and the eng-team pack has no `workspace/` directory to copy from today.

### Composition, extracted once

A new function returns the full topic set the deployment should hold:

```
composed_subscriptions(project_path, extra=()) -> list[str]
```

It is the existing `bobi/service.py:625-642` body, moved: workspace or pack explicit list, then `extra`, then monitor keys, then lifecycle keys, order and dedup preserved.
`service.py` calls it at boot.
The CLI calls it to build its PUT.
`_resubscribe_on_deaf` calls it to build its repair PUT.

This is the fix for N4 and C3 at once.
There is one definition of "what this deployment subscribes to", and the three sites that need it read the same one.

### The CLI

```
bobi agent <name> subscriptions list
bobi agent <name> subscriptions add <topic>...
bobi agent <name> subscriptions remove <topic>...
```

Generic by Zach's own test: the command names a framework concept (a topic), not a domain one (a repo).
It follows the `monitors` group precedent (`bobi/cli.py:3155`).

`add` and `remove` each:

1. Resolve the runtime root via `_detect_project_root` (`bobi/cli.py:80-95`).
2. Rewrite `workspace/subscriptions.yaml` atomically with `atomic_write_text` (`bobi/fsutil.py:100`), preserving the header comment.
3. Build the PUT list with `composed_subscriptions`.
4. Filter it through `authorize_resources(..., filter_unauthorized=True)` so one ungranted topic cannot hard-reject the whole update (N2).
5. Resolve the manager's deployment: `manager_session_name(project_path)` (`bobi/service.py:135-144`) then `load_deployment_state(project_path, session)` (`bobi/events/state.py:44-52`).
6. `PUT /deployments/<id>/subscriptions` with `{"replace": [...], "protocol": protocol_payload()}`.
   The `protocol` key is required; `checkEventProtocol` rejects a request without it.
7. Record the server's reported `subscriptions`, with a timestamp, under `<run>/state/`.
8. Print what changed, and name any topic the client filter dropped or the server 400 reported.

`_put_subscriptions` (`bobi/subagent.py:1926`) cannot be reused: it is a closure over `es_url` defined inside the session function.
The PUT moves to a module-level helper in `bobi/events/server.py` that both the session and the CLI call, so there stays one PUT implementation.

If no deployment state exists the manager is not running.
The CLI still persists the workspace change, then says plainly that nothing was applied live and the change takes effect at next start.
It exits zero: persisting succeeded, and that is the whole operation when there is nothing live to update.

`list` prints the persisted list, the last accepted set with its timestamp, and flags any difference.
It does not PUT, because a read command must not mutate.
The accepted set it shows is the one recorded at step 7, labelled with its age rather than presented as live truth.
See Q1.

### Why the accepted set is cached in `state/` and not `workspace/`

`workspace/` is operator-owned content.
The accepted set is derived from a server response and is meaningless to edit by hand, so it belongs with the other derived per-deployment state next to the cursor and the deployment credentials.

## Exact changes per file

### `moda-labs/bobi-agent`

**`bobi/events/subscriptions.py`**
Add `workspace_subscriptions(project_path) -> list[str] | None`, reusing `_normalize_explicit_subscriptions` (`:13`) and the env interpolation `explicit_subscriptions` applies at `:46-48`.
Add `seed_workspace_subscriptions(project_path) -> None`, write-if-absent.
Add `composed_subscriptions(project_path, extra=())`, lifted from `bobi/service.py:625-642`.
Change the gate at `:61` to consult the workspace layer by presence before the pack layer.

**`bobi/service.py`**
Call `seed_workspace_subscriptions(project_path)` before the read at `:625`.
Replace `:625-642` with one `composed_subscriptions(project_path, extra=extra_subscribe)` call.

**`bobi/events/server.py`**
Add a module-level `put_subscriptions(base_url, deployment_id, api_key, subscriptions) -> list[str]` that PUTs and returns the server's reported `subscriptions`.
Existing `authorize_resources` (`:680-684`) is unchanged.

**`bobi/subagent.py`**
Delete the `_put_subscriptions` closure at `:1926` and call `events.server.put_subscriptions` instead.
Keep `_sync_saved_deployment`'s `filter_unauthorized=False` at `:1883` as is; that choice protects an existing deployment's grants and is not in scope.
Change `_resubscribe_on_deaf` (`:1974-1982`) to build its list from `composed_subscriptions` plus a `filter_unauthorized=True` pass, instead of replaying `active_subscriptions`.
Remove `active_subscriptions` (`:1765`, `:1812`, `:1835`, `:1867`, `:1892`) once nothing reads it.

**`bobi/events/client.py`**
Fix the stale "(and re-registers on failure)" clause at `:501-502`.
Raise the swallowed-exception log at `:518-519` from `debug` to `warning`, so a voided repair PUT is visible (N5).

**`bobi/cli.py`**
Add the `subscriptions` group with `list`, `add`, `remove`, then `main.add_command(subscriptions)`.
Add `"subscriptions"` to the group list at `:4168`, without which `bobi agent <name> subscriptions` does not resolve.
Add it to the pop list at `:4176-4181` so it is not also a top-level command.

**`docs/`**
Document the file and the three commands wherever `subscribe:` is currently documented as pack-only.

### `moda-labs/moda-agents` (companion PR)

**`agents/moda-eng-team/agent.yaml`**
Remove `managed_repos:` (`:149-160`).
Leave `subscribe:` (`:125-132`) in place: it is the seed, and removing it would strand a fresh deployment with nothing to seed from.
Update the `subscribe:` header comment (`:101-124`) to say the list is a seed, not the live source of truth.

**`scripts/check-deploy-compose.py`**
Drop the `managed_repos` assertion at `:85-94`, which cannot pass once the key leaves the pack (N6).
Keep the `subscribe:` assertion at `:76-83`: the seed must still carry moda-skills.

**`agents/moda-eng-team/workspace/managed-repos.yaml`** (new)
The director-maintained tracker bindings, carrying the content removed from `agent.yaml:149-160`.
Seeded by the existing `seed_workspace` (`bobi/install.py:174-192`), which copies pack `workspace/` templates only if absent, so a later install never overwrites director edits.

**`agents/moda-eng-team/roles/director/ROLE.md`**
Read the tracker bindings from `workspace/managed-repos.yaml` and keep that file current.
Resolve the agent name from `BOBI_ROOT` rather than a literal `<name>`: this deployment is installed as `eng-team` while the pack's `agent:` is `moda-eng-team`, so a guessed name picks the wrong one.

## Test plan

The risk here lives in the real event server, so the load-bearing tests are integration tests against it rather than mocks.

**Integration, against the real local event server**

1. `replace` with a changed list returns 200 and the response's `subscriptions` equals the request's; this is the N1 contract the CLI depends on.
2. An ungranted global topic returns `400 unauthorized_topics` naming it, and the deployment's prior subscriptions are unchanged, proving the no-partial-write behavior the design relies on.
3. `{"replace": []}` returns `400 replace[] must not be empty` (N3).
4. A CLI `add` on a running manager changes what the server routes: publish an event on the newly added topic and assert the manager's session receives it, with no restart.
5. A CLI `remove` stops delivery of that topic's events, and the deployment id and api key are unchanged across both, proving identity is preserved and no `register()` happened (C2).
6. After a CLI `add`, the composed set still contains every monitor key and both lifecycle keys, and a published `agent/session.completed` is still delivered (N4). This is the regression test for the largest trap.

**Unit and falsifiable**

7. `workspace_subscriptions` returns `None` for an absent file and `[]` for `subscribe: []`, and `discover_subscriptions` returns `[]` rather than auto-detecting for the latter (C1).
   This test must fail against the current `:61` gate; a version that passes before the change is vacuous and does not count.
8. Seeding is write-if-absent: a second boot does not overwrite an edited file, and a boot with the file present does not touch it.
9. On an upgrade of an existing deployment with no workspace file, the first boot's composed list is byte-identical to the pre-upgrade composed list (migration, below).
10. `_resubscribe_on_deaf` PUTs the persisted list, not a stale in-process one: change the file under a live client, trigger the deaf path, assert the PUT carries the new list (C3).
11. All three read sites agree on one list: `bobi/service.py`, `bobi/supervisor/snapshot.py:107`, and `bobi/ingress.py:83`.
12. `subscriptions add` with no manager running persists the file, reports that nothing was applied live, and exits zero.

## Migration

No behavior change on upgrade.

An existing deployment has no `workspace/subscriptions.yaml`.
The first manager boot after the upgrade seeds it from the installed pack's `subscribe:` and reads it back, so the composed list is identical to what the same boot would have produced before.
`_sync_saved_deployment` then PUTs that identical set and the server reports `added: 0, removed: 0`.
Test 9 asserts exactly this.

The deployment id, api key, and event cursor are untouched: nothing in this change calls `register()` and nothing unlinks a cursor.

### Release ordering

`moda-labs/moda-agents` installs a published wheel, `bobi==${BOBI_VERSION}`, from `.github/fleet-version` (`release-fleet.yml:342`), rewritten by `version-gate.yml:100` after `ci-canary` proves a version.
The pin currently reads `BOBI_VERSION=0.58.0`.

Order:

1. Land and release the bobi-agent change; let the canary prove it and the pin PR land.
2. Land the moda-agents companion PR.
3. Roll the fleet.

Step 2 must not precede step 1.
The new `workspace/managed-repos.yaml` seed relies on `seed_workspace`, which already ships, so D5's pack half has no hard dependency on the new CLI.
But the director prompt would name commands that do not exist yet on a fleet still pinned to 0.58.0, so ordering it after the pin bump keeps the prompt honest.

## Out of scope

- Any repo concept under `bobi/`. No `repos` CLI, no `managed_repos` awareness in framework code (D3, and Zach's August ruling).
- A GET route for live subscriptions. N1 shows the PUT response already carries the accepted set. See Q1.
- Changing `_sync_saved_deployment`'s `filter_unauthorized=False` (`bobi/subagent.py:1883`). That protects an existing deployment's grants and is a separate question.
- Branch-delete safety machinery. Policy and prompt, per D5's accepted trade-off.
- Hot-reloading anything else from the workspace. This change applies exactly one key.
- Making the pack layer presence-gated rather than truthiness-gated. Argued above under "Presence, not truthiness".
- The `BOBI_VERSION=0.58.0` pin lagging the 0.59.0 wheel this container runs. Noticed while verifying release ordering, unrelated to #952.

## Open questions

**Q1. Is a cached accepted set good enough for `subscriptions list`, or should it get a GET route?**
D4 prefers reusing the PUT response, and N1 confirms the PUT returns the accepted set, so no new route is strictly needed.
The cost is that `list` shows "last accepted, as of T" rather than live truth, and nothing refreshes it between mutations.
My recommendation is the cache: it satisfies D4 as written, adds no server surface, and labels its own staleness.
A GET route is the alternative if you want `list` to be authoritative on demand.

**Q2. Should `add` fail or warn when the client filter drops the topic just added?**
A `github:` topic with no resource grant is dropped client-side by `authorize_resources` (N2), so `add` would persist it to the workspace file while the server never routes it.
Warning keeps the workspace list as the operator's declared intent and lets a later grant make it live.
Failing refuses to persist something that does not work today.
My recommendation is to warn loudly and persist, because the grant usually arrives moments later via the GitHub App install, and refusing would make the correct onboarding order fragile.
