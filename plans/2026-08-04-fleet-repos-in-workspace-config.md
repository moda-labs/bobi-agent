# Fleet repo subscriptions in workspace config

Issue: [#953](https://github.com/moda-labs/bobi-agent/issues/953)
Status: spec, awaiting approval (Gate 1)
Date: 2026-08-04

## Problem

Onboarding or offboarding a repo means editing agent pack source in a different
repository and redeploying.

The bindings live in `agents/moda-eng-team/agent.yaml` in `moda-labs/moda-agents`,
installed to `run/package/agent.yaml` as a read-only frozen image, in two lists:

- `subscribe:` routes events (`github:<org>/<repo>`, `linear:<TEAM>`, `slack:...`).
- `managed_repos:` carries a per-repo `tracker:`, and in practice carries write
  authority (see [Authority](#authority-the-part-not-to-skim)).

Zach, in Slack `C0BAEN48KQR` thread `1785885345.077239`:

> we should also move away from having to change the agent pack source to have you
> add or remove repos from management. Can you launch a worker to disconnect the
> pack source from this?

> ok, can we manage subscriptions not in the agent pack itself but rather in
> workspace config? Like when we onboard a new repo, it should subscribe to that
> repo's events

The destination is decided: workspace config. This spec does not re-open that.

## Evidence

Read first-hand at `origin/main` `393221b` and in the live runtime.

### The offboarding cost, measured

`moda-labs/familystories-ai` was offboarded today. Verified timeline, UTC:

| Time | Event |
|---|---|
| 21:21:25 | familystories-ai PR #295 cleanup ran (`wf-pr-closed-eng-team-adhoc-797869b1`) |
| 22:24:09 | offboarding commit `c0ec10a` lands in `moda-labs/moda-agents` |
| 22:37:54 | new pack installed, `run/package/agent.yaml` rewritten |
| 22:46:46 | manager restarted (`state/manager.pid`) |

One repo removed cost a commit in a second repository, a pack rebuild, an install, and a
restart: 22 minutes from merge to effect.

**Correction to the issue's framing.**
The issue implied #295 ran while familystories-ai was absent from both lists.
It did not: the cleanup ran 63 minutes *before* the offboarding commit, so the repo was
still fully listed.
The real defect is the four-step, two-repo change process and the 22-minute window where
source said "offboarded" and the deployment was still subscribed.

### The divergence that still stands

`moda-labs/bobi-agent` and `moda-labs/moda-agents` are in `subscribe:` and absent from
`managed_repos:`. Deliberate, but invisible, and re-derived from scratch by every cleanup
run: `wf-pr-closed-eng-team-adhoc-{68e83099, 0cd4f660, e22ecf8f, ade8b87d,
2d67735b}/handoff-cleanup.yaml`.
`wf-pr-closed-eng-team-adhoc-1bdd41de` found the same shape twice: `moda-agents` is
subscribed with no local clone, so its cleanup is a structural no-op that still reports
"Worktree cleanup complete".

### `managed_repos` has no code consumers

`grep -rn managed_repos` at `origin/main` returns four hits, all unrelated fixture keys in
`tests/test_memory.py` (52, 55, 62, 66). Zero under `bobi/`.
In the installed pack, one hit: `package/agent.yaml:125`, in no prompt or role file.
Agents reach it only by opening `package/agent.yaml` themselves.

The pack comment calls it "Advisory tracker binding read by the director prompt".
Practice promoted it past advisory: the handoffs above treat absence from `managed_repos:`
as absence of write authority and declined deletes on human branches on that basis.
Real, load-bearing, and enforced only by agents reading YAML.

### How a subscription reaches a webhook

1. `bobi/events/subscriptions.py:25` `discover_subscriptions()` reads
   `paths.agent_yaml_path()` = `package_dir(root)/agent.yaml` (`bobi/paths.py:175`),
   returning explicit `subscribe:` when non-empty (`:42`).
   Fallbacks are adapter auto-detection (`:47-55`, which scans local git remotes) and
   finally `[project_path.name]` (`:57`).
2. `bobi/service.py:533` calls it once in `run_manager_from_config`; `:663` passes it to
   `spawn_adhoc()`. Second caller: `bobi/supervisor/snapshot.py:99`.
3. `bobi/events/server.py:609` `authorize_resources()` seeds a per-topic grant. With
   `filter_unauthorized=True` it **silently drops** an ungranted topic and still succeeds.
4. `bobi/events/server.py:760` `register()` POSTs `{name, subscriptions}`.

### What is and is not possible live

`PUT /deployments/<id>/subscriptions {"replace": [...]}` exists and preserves deployment
identity (`bobi/subagent.py:1474`, route at `event-server/worker/src/index.ts:650`).
`register()` is the wrong primitive for a live change: it supersedes the deployment and
mints a new id/key.

But three facts block a naive live watcher, all verified:

- **The drain loop never ticks.** `bobi/events/drain.py:252-253` is
  `while True: event = queue.get()`, a blocking `SimpleQueue.get()` with no timeout. An
  idle manager never wakes, and the case is circular: the new repo cannot produce the
  event that would wake the loop that would subscribe to it.
- **The drain loop is per-session, not per-manager.** `bobi/subagent.py:1546` starts one
  thread per session, and each session owns its own deployment. A watcher there would make
  every worker PUT fleet topics onto its inbox-only deployment.
- **The PUT lives in a closure.** Success sets `active_subscriptions`
  (`bobi/subagent.py:1483`); the deaf-reconnect path replays that variable (`:1510`).
  A PUT that does not update it is reverted on the next reconnect.

**There is also no read-back route.** The event server exposes `POST /deployments`,
`GET /deployments/<id>/subscribe` (WebSocket upgrade only), `PUT .../subscriptions`, and
`DELETE /deployments/<id>`. Nothing returns a deployment's current subscription set to a
third process such as the CLI.

### Workspace config already exists as a mechanism

`bobi/paths.py:207` `workspace_dir()` = `<run>/workspace`; `bobi/install.py:118`
`seed_workspace()` copies pack templates **only if absent** (guard `:134`).
Framework code reading operator-owned files out of the runtime is established
(`bobi/prompts/resolver.py:86-97`, `bobi/monitors/scheduler.py:1318`).
No parallel store is introduced.

## Scope

In scope: a workspace-owned repo registry owning `github:` topics;
`bobi agent <name> repos add|remove|list`; per-repo tracker moved off `managed_repos:`;
migration.

Out of scope, each with a reason:

- **Write authority in workspace config.** See Authority. A decision for Zach; the
  recommendation is not to.
- **`linear:`/`slack:` topics.** Deployment identity, not repo membership. A Linear team
  spans repos and does not follow a repo onboarding.
- **Live reload without a restart.** Blocked by the three facts above. Correct insertion
  point named below as follow-on.
- **Server-side verification of what took effect.** No read-back route exists.
- Revoking resource grants; making write authority code-enforced.

## Design

### The registry file

`<run>/workspace/repos.yaml`:

```yaml
# Fleet repo subscriptions. Authoritative for github: topics.
# Write authority is NOT set here - see managed_repos: in the pack.
repos:
  - repo: underminedsk/lightweave
    tracker: github-issues
  - repo: moda-labs/bobi-agent
    tracker: github-issues
```

Presence means subscribed; there is no `events:` flag and no `write:` flag.
`tracker` defaults to `github-issues`. Values are concrete: no `${VAR}` interpolation,
unlike pack `subscribe:` (`bobi/events/subscriptions.py:40`).
`tracker` does not derive a `linear:<TEAM>` subscription.

**No pack template ships.** The file is created by `repos add` or by the migration below.
Shipping an empty template would be actively harmful: precedence is existence-based, so
`seed_workspace` copying an empty file onto a deployment that has not been migrated would
drop every `github:` topic on install. Absent-by-default also means `seed_workspace` has
nothing to do here, and fleet membership never re-enters pack source.

### The commands

New `@agent.group("repos")` in `bobi/cli.py`, matching `@agent.group("subagents")` (`:1822`).

```
bobi agent <name> repos add <org>/<repo> [--tracker github-issues|linear:<TEAM>]
bobi agent <name> repos remove <org>/<repo>
bobi agent <name> repos list
```

`repos list` shows repo, tracker, and a **write-authority column read from the pack's
`managed_repos:`** so an operator sees in one view that onboarding did not grant write.

There is no `--write` flag. Granting write authority stays a pack PR.

`repos add` validates the `<org>/<repo>` shape and that the GitHub credential can read it,
writes the file atomically (`os.replace`), then restarts the manager session.

**What the command may claim.** It reports what it wrote and that the restart was
performed. It does **not** claim the subscription is live, because no route lets it read
the deployment's actual set back, and `authorize_resources` can drop an ungranted topic
while still succeeding. The command prints that caveat rather than implying confirmation.
Adding a `GET /deployments/<id>/subscriptions` route is the follow-on that would let this
become a real assertion.

### Live effect: a restart is required, and this says so

**A `repos add` takes effect after a manager-session restart. This is not hot reload.**

The reason is the three verified facts above: the drain loop never ticks, it is per-session
anyway, and the PUT lives in a session closure whose `active_subscriptions` would be
replayed over any external update.

What this still buys: a restart is not a redeploy. No pack edit, no second repository, no
rebuild, no install. The 22 minutes in the evidence above was commit-to-install, not the
restart. `repos add` performs the restart itself, so the operator runs one command.

The cost: the restart bounces the manager session. Detached sub-agent sessions are separate
processes and survive, and startup reconcile (`bobi/service.py:642`) re-adopts stranded runs.

The follow-on live path, if wanted later, is a dedicated thread started inside
`_start_event_subscription` so it shares the closure owning `es_deployment`, `es_key`, and
`active_subscriptions`, guarded on `has_external` so workers never run it. It must update
`active_subscriptions` on success, and must not copy the existing failure handling: both
current PUT sites unlink the event cursor and re-register on error
(`bobi/subagent.py:1435`, `:1486`), which on a repeating watcher would replay history into
auto-dispatch on every tick.

### Precedence and upgrade safety

The registry is **authoritative, not merged**, for `github:` topics.

- File exists and parses to a `repos:` list: `github:` topics come from it alone; pack
  `subscribe:` `github:` entries are ignored. This holds for an explicitly empty list,
  which yields zero `github:` topics and must not fall through to adapter auto-detection
  or to the directory-name topic (`bobi/events/subscriptions.py:47-57`) - the former would
  rescan local git remotes and resurrect `run/repo`'s own origin.
- File absent, unreadable, or unparseable: today's behaviour exactly, and a parse failure
  keeps the last good set rather than collapsing to empty.
- Pack `subscribe:` keeps `linear:` and `slack:`.

This designs out the resurrection trap: no union for `github:` topics, so a pack upgrade
still listing an offboarded repo cannot bring it back. A removal is an absence from an
authoritative file and needs no tombstone.

### Authority: the part not to skim

`subscribe` and `managed_repos` are different grants and must stay different.

- `subscribe` routes events.
- `managed_repos` is what a cleanup worker consults before deleting a human's merged
  remote branch. Destructive and irreversible.

Zach's request couples onboarding to subscription. Correct for events, and implemented here.
It must not silently widen delete authority: "onboard this repo" naturally reads as "the
fleet works on it", and one unified entry would hand every new repo the right to delete
human branches.

**Recommendation: write authority stays in the pack**, for a structural reason.
`package/agent.yaml` is hashed into `install-manifest.json` (`bobi/install.py:158-160`), so
`doctor` flags drift; it is overwritten on every install; and changing it requires a
reviewed PR to a second repo.
`workspace/` has none of those properties: excluded from the manifest, never overwritten,
and routinely agent-writable.
Moving the grant there would let an agent grant itself the right to delete human branches
by appending a line to a YAML file. A printed CLI warning does not help, because nothing
forces the grant through the CLI.

**Subscription is not consequence-free either, and this is the thing to flag.**
The live pack's `auto_dispatch` (`package/agent.yaml:95-111`) arms `issue-lifecycle` on
`github.issues.assigned` and `pr-closed` on `github.pull_request` closed, both with
`allow_self_authored: true`, for **every** subscribed repo.
So adding a line to `workspace/repos.yaml` does not merely route events: after the next
restart it auto-dispatches workers, including the branch-deleting cleanup workflow, against
that repo. The only thing standing between that and a destructive act is the same
prompt-enforced `managed_repos` this spec calls unenforced.

That does not block the change, and it is exactly what Zach asked for. It does mean
`workspace/repos.yaml` deserves the same operational care as an authority file even though
it only grants subscription, and it strengthens the case for keeping the authority list
itself in the PR-gated pack.

**Decision for Zach.** His first message said "add or remove repos from management", which
could include authority; his second scoped it to subscriptions. This spec implements the
second and leaves authority in the pack. Moving authority too is a deliberate call with the
consequences above and should be its own change.

### Limitation: grants are not revoked

Resource grants are bubble-scoped and no revoke path exists anywhere in the codebase.
`repos add` seeds a standing bubble-wide grant that `repos remove` does not take back, so
another deployment in the bubble could re-subscribe to an offboarded repo without a fresh
authorization step. The registry is authoritative for **routing**, not authorization.

### Tracker binding

`tracker:` moves into the registry entry; the pack's `managed_repos:` reduces to a pure
authority list of repo names. Pack owns authority, workspace owns subscription and tracker.

Honest parity note: `tracker:` has no code consumer today and gains none here. It reaches
agents exactly as it does now, by an agent opening the file. The director prompt that reads
it lives in `moda-labs/moda-agents` and must be repointed at `workspace/repos.yaml` in the
same change. `--tracker linear:<TEAM>` is accepted and recorded, but Linear *event*
delivery for a new team still requires a pack PR, since `linear:` topics stay pack-side.

## Migration

Written once into the live runtime as `<run>/workspace/repos.yaml`:

```yaml
repos:
  - repo: underminedsk/lightweave
    tracker: github-issues
  - repo: moda-labs/moda-skills
    tracker: github-issues
  - repo: moda-labs/bobi-agent
    tracker: github-issues
  - repo: moda-labs/moda-agents
    tracker: github-issues
```

The pack's `managed_repos:` keeps `underminedsk/lightweave` and `moda-labs/moda-skills`, so
today's decline behaviour on `bobi-agent` and `moda-agents` human branches is preserved
exactly. No authority changes.

`linear:MDS`, `linear:MOD`, and the `slack:` topic stay in pack `subscribe:`, so no delivery
is lost.

`moda-labs/familystories-ai` is **not** in the registry. It was deliberately offboarded
today at 22:24 UTC in `c0ec10a`, because baohua is its sole engineering team. The registry
records that as an absence, and authoritative-not-merged means a pack upgrade cannot
resurrect it. The stale checkout at `run/checkouts/familystories-ai` is left alone.

Ordering matters: the migration file must exist **before** the framework change is deployed,
because precedence is existence-based. After this has run, the pack's `github:` `subscribe:`
entries are deleted in a follow-up PR to `moda-labs/moda-agents`.

## Verification plan

- Unit: registry parse; `tracker` default; malformed entries rejected loudly; no `${VAR}`
  interpolation; parse failure keeps last good rather than collapsing to empty.
- Unit: `discover_subscriptions` returns registry `github:` topics when the file exists,
  pack `subscribe:` when absent, and never merges the two classes.
- Unit: an explicitly **empty** registry yields zero `github:` topics and falls through to
  neither adapter auto-detection nor the directory-name topic.
- Unit: `tracker: linear:MOD` adds no `linear:` subscription.
- Regression, resurrection trap: a pack upgrade still listing an offboarded repo in
  `subscribe:` does not resubscribe it.
- Regression, authority trap: nothing in the registry path writes or infers
  `managed_repos:`; `repos add` leaves pack authority byte-identical.
- Regression, the install trap: installing a pack onto a runtime with no registry does not
  change the effective topic set.
- Unit: `supervisor/snapshot.py:99` reports the registry-derived set.
- Integration: `repos add` / `list` / `remove` against a temp runtime root, asserting file
  contents and effective topics at each step, and that the file write is atomic.
- Integration against the real dependency: drive the local event server through a full
  manager start and assert the deployment's registered subscription set matches the
  registry, and that a removed repo is genuinely unsubscribed rather than merely absent
  from the file. The #488 grant path is where the risk lives, so this one is not a mock.

## Implementation plan

1. `bobi/fleet.py`: load, validate, atomically write `workspace/repos.yaml`; derive
   `github:` topics.
2. `bobi/events/subscriptions.py`: registry owns `github:` when the file exists, including
   the empty case; pack keeps `linear:`/`slack:`; parse failure keeps last good.
3. `bobi/cli.py`: `@agent.group("repos")` with `add`, `remove`, `list`, the pack-sourced
   authority column, the restart, and the honest no-confirmation caveat.
4. Migration file written into the live runtime, before deploy.
5. Docs: `docs/BUILDING_AGENT_TEAMS.md` gains a repo-registry section. Nothing to rewrite:
   `managed_repos` does not currently appear in `docs/` at all.
6. Companion PR in `moda-labs/moda-agents`: repoint the director prompt at
   `workspace/repos.yaml`, reduce `managed_repos:` to an authority list.

## Review status

**Same-model adversarial pass: two rounds, run, and this is the twice-revised document.**

Round 1 returned 2 blockers and 6 major findings. The design changed rather than absorbing
them: write authority stayed in the pack (was a `write:` field in workspace), `linear:`
derivation was dropped (it contradicted the precedence rule), the `events:` field was
dropped, the `doctor` check was cut as noise-by-construction, and the restart was replaced
with a live file-watch.

Round 2 killed that file-watch with 3 further blockers, all verified first-hand here: the
drain loop never ticks, it is per-session, and there is no read-back route. So this version
returns to an explicit restart, drops the read-back claim, ships **no** pack template
(an empty one would unsubscribe an unmigrated fleet on install), and adds the
`auto_dispatch` finding, which materially changes the risk picture and is the single most
important thing for a reviewer to look at.

A citation pass ran separately across both rounds and found four wrong line references, all
corrected. One was wrong because `run/repo` sits on a parked WIP HEAD whose unmerged edit to
`docs/BUILDING_AGENT_TEAMS.md` does not exist on `origin/main` - a reminder to cite against
the ref, not the working tree.

**Cross-model pass: still owed.**
`codex exec` was run in this container and returned
`401 Unauthorized: Missing bearer or basic authentication` against
`https://api.openai.com/v1/responses`; there is no fallback LLM key.
The same-model pass is a fallback, not a substitute. Not faked, not waived.
