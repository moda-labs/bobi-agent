# Workspace-owned event subscriptions

Issue: [#952](https://github.com/moda-labs/bobi-agent/issues/952)
Status: spec, awaiting approval (Gate 1).
Written 2026-10-09 from scratch, replacing the generic-overlay design in PR #956.

Every `bobi/`, `event-server/`, `tests/`, `docs/` and `AGENTS.md` file:line below was read at `83bebe49` (`origin/main`, 2026-10-09).
Every `moda-agents` file:line was read at `bf9d1088` (`origin/main`, 2026-10-08).
Line numbers from the August review rounds have moved and are not reused.

Revision 3, after two independent review rounds:
- `plans/reviews/2026-10-09-952-subscriptions-review-1.md` (3 blockers; the design lost its composition function as a result).
- `plans/reviews/2026-10-09-952-subscriptions-review-2.md` (2 blockers; the accepted set moved off the credential record, and seeding stopped firing for auto-detecting teams).

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
`docs/EVENT_SERVER.md:396-398` states the same composition, including "the session's own `inbox/<self>`".

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
Three existing tests also assert that document's exact shape (`tests/test_event_subscription.py:71`, `:469-471`, `:514-515`), and two of them exist to prove a successful sync does NOT rewrite it.

A third per-session file follows the pattern the other two already set.

`event-server/core/src/core.ts:1537-1540` returns `{subscriptions, added, removed, protocol}` on a `replace`, and `bobi/subagent.py:1938-1946` reads that body, validates it, and throws it away.
The data D4 asks for is already on the wire, one line from where it would be written.

The register path is different and must be described differently.
`register()` (`bobi/events/server.py:849-875`) returns `(deployment_id, api_key)` only; the response body (`core.ts:1473-1480`) carries no `subscriptions` member.
What a 201 proves is that the server stored the requested array verbatim (`core.ts:1461`), having rejected the whole request otherwise, so the register site records the list it passed in rather than a server echo.

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

### Most packs have no `subscribe:` at all, so presence cannot be seeded blindly

`explicit_subscriptions` returns `[]` for an absent `subscribe:` key (`bobi/events/subscriptions.py:47`), for an absent `agent.yaml` (`:41-42`), and for a literal `subscribe: []`.
It cannot distinguish them, so a seeder that writes whatever it returns would write an authoritative empty list for every auto-detecting team.

That is the majority of the fleet, derived mechanically at `bf9d1088`:

```
$ for f in agents/*/agent.yaml; do echo "$(grep -c '^subscribe:' $f)  $f"; done
1  agents/baohua/agent.yaml
0  agents/gtm-team/agent.yaml
0  agents/market-research/agent.yaml
1  agents/moda-eng-team/agent.yaml
0  agents/roadmap-pm/agent.yaml
0  agents/support-manager/agent.yaml
0  agents/zachs-personal-assistant/agent.yaml
```

Four of those five have `events: true` services, bobi-agent ships no `agent.yaml` template with `subscribe:`, and `docs/BUILDING_AGENT_TEAMS.md:129-130` calls omitting it "preferred".
Both manager-booting test fixtures are in that state.

Seeding must therefore be conditional on the pack declaring a non-empty list.

### The ingress warning reads a different function, and `doctor` reads it too

`bobi/ingress.py:83` reads `explicit_subscriptions`, not `discover_subscriptions`.
`tests/test_ingress.py:260-276` pins the two to one parser, and the `explicit_subscriptions` docstring at `:28` names itself "The ONE parser for the `subscribe:` surface (D078)".

A workspace layer added only to `discover_subscriptions` would silently break that invariant while the test stayed green, because the test writes no workspace file.
The workspace layer goes inside `explicit_subscriptions`.

That makes `bobi doctor` a reader too, through `check_ingress_reachability`: `bobi/doctor.py:51` runs the check and `:669-678` catches every exception into `CheckResult(ok=True, detail=f"skipped: {exc}")`.
A malformed workspace file would therefore fail the manager boot while `doctor`, the command an operator runs first, reported green.

### Authorization is a network write, it fails open, and it covers two services

`authorize_resources` (`bobi/events/server.py:680-684`) is not a filter.
It POSTs the real GitHub/Linear credential to `/resources/authorize` once per global topic (`:650-655`, `:781-784`), needs the signed bubble identity from `<run>/state/bubble.json`, and returns the list UNFILTERED when that identity is absent (`:713-714`: "can't sign - leave the set unchanged").

It writes grants for two services only: `_RESOURCE_CRED_KEYS` at `:640` is `{"github": ..., "linear": ...}`, and `:761-765` passes `slack:`/`whatsapp:`/`discord:` through untouched because their grants come from the channel registrations in `_register_channel_credentials` (`bobi/subagent.py:1767`), which runs at session start.
The server checks all five prefixes (`core.ts:411`, `:1504-1512`), and the slack grant is keyed on the TEAM id rather than the channel (`core.ts:445-450`), so a new channel inside an already-registered workspace passes while a new workspace does not.

Its `filter_unauthorized=False` mode is the deliberate policy for an existing deployment, and both the function docstring (`:706-712`) and `_sync_saved_deployment` (`bobi/subagent.py:1859-1864`) give the same reason: "replacing the deployment's subscriptions with a filtered list would silently unsubscribe a valid existing deployment."
The CLI PUTs to that same deployment, so it uses the same mode.
`filter_unauthorized=True` there would drop a topic whose credential has since rotated out of the container env while the server still holds a no-expiry grant, removing it from the live index as a side effect of an unrelated `add`.

Grants are permanent: `event-server/worker/src/index.ts:147-165` writes them with no `expirationTtl`, and `core.ts` has no revocation route.
That is what makes a `remove` safe without re-authorizing, since the server re-checks every surviving topic in the `replace` array (`core.ts:1509`).

### The server rejects; it does not silently drop

`core.ts:1508-1512` calls `unauthorizedGlobalTopics` and, on any ungranted topic, returns `400 {"error":"unauthorized_topics","topics":[...]}`, rejecting the whole update with no partial write.
`core.ts:1498-1500` returns `400 replace[] must not be empty`, so an empty replace is never a legal repair, which matters for the fallback rules below.

The silent drop is client-side, in `authorize_resources` with `filter_unauthorized=True`, which only the register path uses (`bobi/subagent.py:1805-1808`).
So D4's "topics the server silently dropped" is two different things, and with `filter_unauthorized=False` on the new path only the first applies: a 400 naming the exact topics.
Neither needs a GET route.

The register path already parses that 400 into `UnauthorizedTopics` (`bobi/events/server.py:627`, raised at `:838-842`).
The PUT path does not: `bobi/subagent.py:1938-1946` reaches `resp.raise_for_status()` first and discards the topic list.

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

One new workspace file, one new CLI group, one new per-session state file.
No composition function, no change to `bobi/service.py`'s composition, no new server route, no overlay, no merge semantics, and one existing error class reused rather than a new one.

### The file

`<run>/workspace/subscriptions.yaml`, a mapping with one key:

```yaml
# Event topics this team subscribes to.
# Seeded from the agent pack on first manager boot; the pack is only the seed.
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

That body is the seed this team gets, taken from `agents/moda-eng-team/agent.yaml:128-134` at `bf9d1088`.

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
A malformed file raises, and the exception carries the file path.

`explicit_subscriptions` consults it first and returns it when it is not `None`, so the one parser keeps serving both its callers and the ingress warning at `bobi/ingress.py:83` follows the workspace layer for free.

`discover_subscriptions` calls `workspace_subscriptions` directly, BEFORE and OUTSIDE the `try` at `:59-66`, and returns the list when it is not `None`.
That placement is the whole point.
Inside the `try`, a malformed workspace file would be swallowed into auto-detection; outside it, the manager boot fails with the path, while a malformed PACK `agent.yaml` keeps its historical fall-through, which `tests/test_ingress.py:306-324` pins.
That test monkeypatches `explicit_subscriptions`, so `discover_subscriptions` must keep calling it inside the `try` rather than reaching past it to a pack-only reader; otherwise the pin stays green while no longer proving anything.

The file is therefore read twice on a boot where it is absent, once by each function, which costs one extra `Path.exists()`.
One window follows from that: a file CREATED between the two reads is read from inside the `try`, so if it is also malformed the exception is swallowed for that one boot.
The CLI writes with `atomic_write_text`, so this needs a create rather than a torn write, and the next boot fails loud as intended.

The pack's own truthiness gate at `:61` is left alone, deliberately.
Nothing writes `package/agent.yaml` at runtime, so an empty pack list is not a reachable operator mistake, and `tests/test_symmetric_node.py:90-102` pins the current behavior.
(The `monitors` commands DO write `package/monitors.yaml` under `with_mutable_runtime_package`; `agent.yaml` has no such path.)

The three other readers degrade safely and are unchanged, except for `doctor`.
`bobi/supervisor/snapshot.py:104-109` wraps its `discover_subscriptions` call in `except Exception: pass` and is documented as best-effort, so a malformed file yields no declared subscriptions and silence detection simply asserts less.
`bobi/service.py:184-195` wraps `check_ingress_reachability` the same way, so the reachability warning is skipped at `log.debug`.
`bobi/doctor.py:669-678` must change: a malformed workspace subscriptions file makes the ingress check FAIL with the path, instead of landing in the `ok=True, detail="skipped: ..."` arm that would hide the one thing the operator is looking for.

### Seeding

One call at the start of the manager boot path in `bobi/service.py`, before the read at `:625`.
It writes `<run>/workspace/subscriptions.yaml` only when the file is absent AND the installed pack declares a non-empty `subscribe:`.

A pack with no `subscribe:` gets no file, so an auto-detecting team keeps auto-detecting and nothing about its boot changes.
The file exists only for a team that declared a seed or ran `subscriptions add`, and presence therefore means exactly what the pack's truthy `subscribe:` means today: an explicit list exists.

The manager is not the only writer, so every writer goes through one locked helper.
`bobi/fsutil.py:162` `file_lock` is held across load, mutate and save; `AGENTS.md:97` requires it for read-modify-write state, and `atomic_write_text` alone keeps the file parseable without stopping a concurrent updater's change from being overwritten.
Seeding takes the same lock, so an `add` racing a cold start cannot be lost.

Seeding at boot rather than in `seed_workspace` (`bobi/install.py:174-192`) is deliberate.
`seed_workspace` copies pack `workspace/` templates and knows nothing about `subscribe:`; teaching it a specific key would break the "general purpose" ruling, and the eng-team pack has no `workspace/` directory to copy from today.

### The accepted set, recorded per session

A third per-session state file, beside the deployment record and the cursor:

```
<run>/state/subscriptions/<session>.json   ->  {"subscriptions": [...], "at": "<iso8601>"}
```

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
2. Under `file_lock`, load `workspace/subscriptions.yaml`, apply the topic edit, and write it back with `atomic_write_text` (`bobi/fsutil.py:100`).
   When the file is absent, seed it from `discover_subscriptions(project_path)` before applying the edit, so a team that was auto-detecting keeps its effective set instead of collapsing to the one topic named on the command line.
   That read can make live Slack and Linear API calls on such a team, once, which is stated in the command's own output.
3. Resolve the manager's session and its live identity: `manager_session_name(project_path)` (`bobi/service.py:135-144`), then `load_deployment_state(project_path, session)` (`bobi/events/state.py:44-52`) and `load_accepted_subscriptions(project_path, session)`.
   If either is missing, stop here: report that nothing was applied live and exit zero (see below).
   Resolving this first is what keeps a persist-only run from doing any network or process work.
4. Build the PUT list by applying the SAME topic edit to the recorded accepted set.
   Nothing is recomposed, so monitor, lifecycle and `inbox/` topics pass through untouched by construction.
5. For `add` only, call `authorize_resources(..., filter_unauthorized=False)` so a newly added `github:` or `linear:` topic has its grant written before the PUT.
   If `load_bubble_state` (`bobi/events/state.py:79`) is empty the command FAILS with that fact, rather than proceeding into the unfiltered pass-through at `bobi/events/server.py:713-714`.
   `remove` skips it: the server re-checks every surviving topic (`core.ts:1509`), but grants carry no TTL and have no revocation route, so a survivor granted at an earlier boot still passes.
6. Resolve `es_url` from `cfg.event_server_url` with the `http://localhost:8080` fallback, and `ensure_running` when `local_port_from_url` matches, exactly as `bobi/subagent.py:1948-1955` does.
7. `PUT /deployments/<id>/subscriptions` via the shared `put_subscriptions` helper.
8. Record the returned set with `save_accepted_subscriptions`.
9. Print the full diff of the previous accepted set against the new one, naming every add and every remove, not just the topic the operator typed.
   On `UnauthorizedTopics`, print the topics the server named and state that nothing was applied live.

Three states mean "nothing live to update": no deployment record (the manager is not running), a deployment record with no accepted-set file (the running manager predates this version), and a `put_subscriptions` rejection.
In the first two the CLI still persists the workspace change, says plainly that nothing was applied live and that the change takes effect at next start, and exits zero, because persisting succeeded and that is the whole operation.
In the third it exits non-zero, so a script does not read a rejected apply as success.

`list` prints the persisted workspace list and the recorded accepted set with its timestamp, and flags any difference.
With no record, or a record the reader returns `None` for, it prints the persisted list and says no live set is recorded, naming which of the two reasons applies.
It does not PUT, because a read command must not mutate.
See Q1.

The workspace file and the live set can diverge, and that is detectable rather than hidden.
An `add` issued while the manager is booting can lose the race: the boot PUT is built from the workspace file, so if the CLI's write lands first the topic is live, and if it lands second the file is ahead of the server until the next start.
`list` reports exactly that gap, which is what D4's read-back is for.

## Exact changes per file

### `moda-labs/bobi-agent`

**`bobi/events/subscriptions.py`**
Add `workspace_subscriptions(project_path) -> list[str] | None`, reusing `_normalize_explicit_subscriptions` (`:13`) and the env interpolation `explicit_subscriptions` applies at `:46-48`; a parse error raises with the path.
Add `seed_workspace_subscriptions(project_path) -> None`, write-if-absent and only when the pack's list is non-empty, under `file_lock`.
Have `explicit_subscriptions` (`:25`) return the workspace list when it is not `None`.
Have `discover_subscriptions` (`:51`) consult `workspace_subscriptions` before and outside the `try` at `:59-66`, gating on `is not None`.
Leave the pack gate at `:61` unchanged.

**`bobi/service.py`**
Call `seed_workspace_subscriptions(project_path)` before the read at `:625`.
Nothing else changes; `:625-642` stays as it is.

**`bobi/events/state.py`**
Add `accepted_subscriptions_path`, `load_accepted_subscriptions` and `save_accepted_subscriptions`, following `session_cursor_path` (`:37`) for the path shape and `atomic_write_json` for the write.
`save_deployment_state` (`:55-64`) is unchanged.

**`bobi/events/server.py`**
Add a module-level `put_subscriptions(base_url, deployment_id, api_key, subscriptions) -> list[str] | None`.
It is the closure at `bobi/subagent.py:1926-1946` moved out, same contract: `bobi/http.put` (`:78`) with `timeout=10.0`, body `{"replace": [...], "protocol": protocol_payload()}`, then `raise_for_protocol_error`, then an `UnauthorizedTopics` raise on `400 {"error":"unauthorized_topics"}` matching the register path at `:838-842`, then `raise_for_status`, the `"error" in data` guard, and `validate_server_response`.
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
`_check_ingress_reachability` (`:669-678`) returns `ok=False` with the file path when the failure is a malformed workspace subscriptions file, instead of the blanket `ok=True, detail="skipped: ..."`.

**`bobi/cli.py`**
Add the `subscriptions` group with `list`, `add`, `remove`, then `main.add_command(subscriptions)`.
Add `"subscriptions"` to the group list at `:4168`, without which `bobi agent <name> subscriptions` does not resolve.
Add it to the pop list at `:4177-4179` so it is not also a top-level command.

**`tests/test_event_subscription.py`**
The three exact-shape assertions on the deployment record (`:71`, `:469-471`, `:514-515`) stay as they are, since the credential document does not change.
`:469-471` and `:514-515` additionally assert the new accepted-set file now exists, which is what their "the saved deployment survives" docstrings are about.

**Docs**
`docs/EVENT_SERVER.md:378-398`: the resolution order gains a level above "Explicit", and the "on top of that" paragraph at `:396-398` stays correct but now describes a list seeded from the pack.
`docs/BUILDING_AGENT_TEAMS.md:129-133`: `subscribe:` stops being the live source of truth and becomes the seed, and omitting it still means auto-detection.
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
2. A CLI `add` on a running manager changes what the server routes: publish an event on the newly added topic and assert the manager's session receives it, with no restart.
3. A CLI `remove` stops delivery of that topic's events, and the deployment id and api key are unchanged across both, proving identity is preserved and no `register()` happened.
4. After a CLI `add`, the live set still contains every monitor key, both lifecycle keys, and `inbox/<manager session>`: a published `agent/session.completed` is still delivered, and `bobi agent <name> message` still reaches the manager.
   This is the regression test for the largest trap.
5. A CLI `add` does not change monitor delivery when `monitors pause` ran since the last boot: pause a monitor, `add` an unrelated topic, and assert the paused monitor's topic is still in the live set.
6. A `remove` whose survivors include a `github:` topic granted at a prior boot returns 200 with no re-authorization, pinning the no-TTL assumption step 5 rests on.
7. Cross-session isolation survives the change: on the `test_event_isolation.py` harness, a worker's deployment never gains a `github:` topic while the manager's CLI change is applied.

**Unit, on the deaf-reconnect seam**

The deaf path is entered only by the heartbeat detector after `_HEARTBEAT_TIMEOUT_S = 95.0` seconds of no pong (`bobi/events/client.py:260`, `:294-297`), and the only harness that forces it is the fake-WebSocket fixture at `tests/test_event_client_heartbeat.py:144-165`.
These two therefore drive `on_deaf_reconnect` off a patched client, exactly as `tests/test_event_subscription.py:110-136` already does.

8. A worker session's deaf reconnect PUTs only `inbox/<worker>`, never a `github:`/`slack:`/`linear:` topic.
9. A manager's deaf reconnect after a CLI change PUTs the recorded set, not the boot-time list.
10. A PUT whose 200 body omits `subscriptions` leaves the record untouched, and the next deaf reconnect still replays a non-empty set rather than `{"replace": []}`.

**Unit, on the subscription surface**

11. With `workspace/subscriptions.yaml` present and `subscribe: []`, `discover_subscriptions` returns `[]`.
    Written against the pre-change code this is red for the right reason: the pre-change gate ignores the file entirely and returns the pack list.
    A version that passes before the gate changes is vacuous and does not count.
12. A malformed `workspace/subscriptions.yaml` does NOT produce an auto-detected set.
    Asserted against the real detector rather than a mock, so the test fails if the read is ever moved inside the `try` at `:59-66`.
    The same test asserts `bobi/supervisor/snapshot.py:104-109` still returns an empty expectation list rather than raising, and that `bobi doctor`'s ingress check reports `ok=False` with the path.
13. `explicit_subscriptions` and `discover_subscriptions` agree with a workspace file present, extending `tests/test_ingress.py:260-276` so the D078 pin actually covers the new precedence.
14. `workspace_subscriptions` returns `None` for an absent file and `[]` for `subscribe: []`.
15. Seeding is write-if-absent AND pack-non-empty: a pack with no `subscribe:` produces no file and keeps auto-detecting, a pack with a list produces the file once, and a second boot does not overwrite an edited file.
16. Two concurrent `add` calls both survive, and an `add` racing boot seeding is not lost.
17. `subscriptions add` on a team with no workspace file seeds it from the effective set, so the previously auto-detected topics are still present afterwards.
18. `subscriptions add` with no manager running, and with a deployment record but no accepted-set file, persists the file, reports that nothing was applied live, and exits zero.
19. `subscriptions add` with an empty bubble state fails with that reason and does not PUT.
20. On an upgrade of an existing deployment whose pack declares `subscribe:`, the first boot's composed list is byte-identical to the pre-upgrade composed list.

Two assertions are deliberately NOT new tests.
`replace` returning the accepted set is already covered at `event-server/test/core.spec.ts:2284-2306` (`added: 1`, `removed: 1`, the exact `subscriptions` array) and over real HTTP at `tests/integration/test_event_server.py:510-520`.
`{"replace": []}` returning 400 is already covered at `core.spec.ts:2314-2324`.

`bobi/service.py`'s composition does not move, so `tests/test_service.py:21-26`, which monkeypatches `subscriptions.discover_subscriptions`, `monitor_subscription_keys`, `lifecycle_subscription_keys` and `MonitorRegistry.load` as module attributes, keeps working untouched.

## Migration

No behavior change on upgrade, in both pack shapes.

A pack with no `subscribe:` gets no workspace file, so nothing about its boot or its auto-detection changes.
That is 5 of 7 fleet packs.

A pack with a `subscribe:` list gets the file seeded on the first manager boot after the upgrade, from that same list, so the composed list is identical to what the same boot would have produced before.
`_sync_saved_deployment` then PUTs that identical set, the server reports `added: 0, removed: 0`, and that response becomes the first recorded accepted set.
Test 20 asserts the first half; test 18 covers the window before that boot, when the CLI persists but cannot apply.

The deployment id, api key, and event cursor are untouched: nothing in this change calls `register()`, nothing unlinks a cursor, and the credential record is not rewritten.

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

- Any repo concept under `bobi/`, per D3 and Zach's August ruling.
- A GET route for live subscriptions.
  The PUT response already carries the accepted set and is now recorded.
  See Q1.
- Making a `whatsapp:`, `discord:` or new-workspace `slack:` topic apply LIVE from the CLI.
  Their grants are written by the channel registrations the manager runs at session start (`bobi/subagent.py:1767`), which the CLI does not run, so such a topic persists to the workspace file and applies at the next start.
  An in-workspace `slack:` channel does apply live, because the grant is keyed on the team id.
- Changing `_sync_saved_deployment`'s `filter_unauthorized=False` (`bobi/subagent.py:1883`).
  The CLI now matches it rather than diverging from it.
- Making the pack layer presence-gated rather than truthiness-gated.
  Argued above under "Presence, not truthiness".
- Branch-delete safety machinery.
  Policy and prompt, per D5's accepted trade-off.
- Hot-reloading anything else from the workspace.
  This change applies exactly one key.
- A pure grant-check that does not write.
  `authorize_resources` always attempts authorization, and `filter_unauthorized=False` leaves the server authoritative, which is enough here.

## Open questions

**Q1. Is a recorded accepted set good enough for `subscriptions list`, or should it get a GET route?**
Three options, and D4 prefers the first.
(a) Record what the last PUT or register accepted, and have `list` show "last accepted, as of T" beside the persisted list.
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
