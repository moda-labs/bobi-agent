# Runtime repo registry: onboard and offboard repos without touching the pack

Status: **Draft - awaiting approval**
Issue: [#952](https://github.com/moda-labs/bobi-agent/issues/952)
Requested by: Zach, Slack `C0BAEN48KQR` thread `1785885345.077239`, 2026-08-04

---

## 1. Problem

Repo bindings are baked into pack source.
Onboarding or offboarding a repo means editing `agent.yaml` and redeploying the team.

Two lists carry those bindings, composed from the overlay
(`moda-labs/moda-agents` `agents/moda-eng-team/agent.yaml`) onto core
(`moda-labs/bobi-agent` `agents/eng-team/agent.yaml`) and flattened into
`<run>/package/agent.yaml`:

- `subscribe:` - event topics (`github:<org>/<repo>`, `linear:<TEAM>`, `slack:...`)
- `managed_repos:` - per-repo `tracker:` binding

Zach's two messages set both the problem and the destination:

> we should also move away from having to change the agent pack source to have you
> add or remove repos from management. Can you launch a worker to disconnect the
> pack source from this?

> ok, can we manage subscriptions not in the agent pack itself but rather in
> workspace config? Like when we onboard a new repo, it should subscribe to that
> repo's events

Destination is decided: **workspace config**. This spec does not re-open that.

### 1.1 The divergence is already live

`<run>/package/agent.yaml` (installed, v1.16.0) declares four GitHub subscriptions
and two managed repos. Against that:

| Repo | `subscribe:` | `managed_repos:` | Events actually arriving |
|---|---|---|---|
| `underminedsk/lightweave` | yes | yes | yes |
| `moda-labs/moda-skills` | yes | yes | yes |
| `moda-labs/bobi-agent` | yes | **no** | yes, continuously |
| `moda-labs/moda-agents` | yes | **no** | yes, continuously |
| `moda-labs/familystories-ai` | **no** | **no** | **yes** |

`familystories-ai` was dropped from both lists on 2026-08-03 and its events still
arrive. Verified first-hand: session `wf-pr-closed-eng-team-adhoc-797869b1`
(`<run>/state/sessions/.../state.json`) carries
`"PR #295 in moda-labs/familystories-ai closed (merged=True)"` with
`started_at: 1785878292` = 2026-08-04 21:21 UTC. A `<run>/checkouts/familystories-ai`
clone is live on the host.

### 1.1.1 Root cause of that row, confirmed

Not a mystery, and it is exactly the bug this spec fixes.

The director's saved deployment is `5307cf5a-...`
(`<run>/state/deployments/bobi-eng-team-director.json`, mtime **2026-07-25**).
Its event log `<run>/state/events-5307cf5a-....jsonl` holds **3,155**
`familystories-ai` events, the most recent at **2026-08-04 23:27:50**.
`<run>/package/agent.yaml` was reinstalled at **2026-08-04 22:37:54**, already without
`familystories-ai`.

So events for a repo absent from the installed pack were delivered **50 minutes after
that pack was installed**. The server-side subscription set is registered once per
manager-session start (§2.4) and has not been replaced since the deployment was
created. The overlay's own warning - "this team must be redeployed for the cut to take
effect" - is not a caveat, it is a live outage of intent.

A second, related gap: `deployments/bobi-eng-team-director.json` holds only
`{deployment_id, api_key}`. Nothing on disk records what the server believes this
deployment is subscribed to, so the drift is invisible locally. D3 carries a
requirement for that.

### 1.2 The consequence is not just inconvenience

`<run>/state/long_term_memory.md` line 8 reads, verbatim:

> Managed GitHub repos (tracker `github-issues`, declared in `package/agent.yaml`):
> `moda-labs/bobi-agent` (primary; renamed `modastack`), `moda-labs/familystories-ai`,
> `moda-labs/moda-skills`, `moda-labs/moda-agents`, `underminedsk/lightweave`.

Five repos, attributed to a file that declares two, erring permissive on the list
that governs **write authority**.
Cleanup workers deleted three `moda-agents` remote branches on the permissive
reading (moda-agents #68, #71, #90, all since restored) against four correct
declines (#78, #80, #84, #92).

Every cleanup run currently re-derives write authority by hand because no
instrument answers "is this repo managed" authoritatively.
The registry is not merely awkward to edit. It is not trusted.

---

## 2. What the code actually does today

Every claim below was read first-hand in this repo at `393221b`.

### 2.1 The pack is read-only at runtime

`bobi/runtime_guard.py:142-152` registers `<run>/package/` as a `"team-package"`
protected root; `apply_runtime_write_policy` (`:178-190`) chmods the tree read-only.
Confirmed empirically on the live host:

```
dr-xr-xr-x  /data/.bobi/agents/eng-team/run/package
-r--r--r--  /data/.bobi/agents/eng-team/run/package/agent.yaml
drwxr-xr-x  /data/.bobi/agents/eng-team/run/workspace
$ touch run/package/.wtest  ->  Permission denied
```

There is an unlock hatch, `with_mutable_runtime_package()` (`runtime_guard.py:239-248`),
used by `install.py:42,182` and by `bobi ... monitors add/pause/remove`
(`cli.py:2537,2565,2592`).

### 2.2 A reinstall rewrites `agent.yaml` unconditionally

`bobi/install.py:39-77` recomposes the image "verbatim from the pack source every
time, including agent.yaml", ending in `_write_yaml(dest / "agent.yaml", cfg)` (`:73`).
Contributed surface directories are `shutil.rmtree`'d and recomposed (`:56-59`).

**Writing runtime config into `package/agent.yaml` is therefore self-erasing.**
The `monitors.yaml` precedent survives only because no layer contributes a file at
that exact path, which is an accident of the rmtree list, not a guarantee.

### 2.3 Workspace is writable and never overwritten

`bobi/paths.py:207-208` resolves `<run>/workspace/`.
`bobi/install.py:118-136` `seed_workspace()`:

> Workspace files are user-owned domain content [...] each file is copied only if
> absent - reinstall never overwrites user edits.

Confirmed writable on the live host (`drwxr-xr-x`, `touch` succeeds).
This is the property that makes Zach's chosen destination structurally correct,
and it is existing behaviour, not something this work must build.

### 2.4 Subscriptions are computed once, at boot

- `bobi/service.py:533` - `run_manager_from_config()` calls `discover_subscriptions(project_path)`
- `bobi/events/subscriptions.py:34-43` - reads `paths.agent_yaml_path()` and returns
  the explicit `subscribe:` list verbatim when non-empty
- `bobi/service.py:656` - passes it to `spawn_adhoc(..., subscribe=subscribe)`
- `bobi/subagent.py:795` - `spawn_adhoc` constructs the `Session` **in-process**
- `bobi/session.py:1571-1606` - `Session._start_subscription()` builds `keys` and calls
  `_start_event_subscription(self.name, keys, bobi_root(), register_attempts=1)`

Nothing re-reads the list while the manager runs.
The overlay's own comment states the consequence: "this team must be redeployed for
the cut to take effect."

### 2.5 The live re-wire API already exists

Three call sites in `_start_event_subscription` PUT a whole subscription set to a
running deployment:

```python
resp = pooled.put(
    f"{es_url}/deployments/{es_deployment}/subscriptions",
    json={"replace": authorized},
    headers={"Authorization": f"Bearer {es_key}", ...},
)
```

- `subagent.py:1422` and `:1473` - the saved-deployment sync branch (local-server and
  remote paths). Both run `authorize_resources()` first, then PUT, then set
  `active_subscriptions = list(authorized)`.
- `subagent.py:1500-1517` - `_resubscribe_on_deaf()`, a closure passed to
  `EventServerClient` as `on_deaf_reconnect`, which re-asserts
  `{"replace": active_subscriptions}` after a forced reconnect.

`replace` is a whole-set swap, so it handles additions **and** removals by
construction. Today all three run only at session start or on reconnect.

`authorize_resources()` (`bobi/events/server.py:609-640`) must run before any
register/PUT to obtain the #488 bubble-scoped grant for `github:`/`linear:` topics;
an unauthorized topic hard-rejects the whole atomic PUT.

`bobi/session.py:1613-1660` already re-invokes `_start_event_subscription` from a
background daemon thread on retry, so re-registration at runtime is a path the code
already exercises.

**The gap:** `active_subscriptions` is a closure-local list inside
`_start_event_subscription`, and the `Subscription` handle it returns
(`subagent.py:1186-1198`) carries only `client`, `drain_thread`, `queue`. There is no
way for an outside caller to change a live session's subscription set. Closing that
is the one genuinely new piece of plumbing this work needs (D4).

### 2.6 `managed_repos` is read by zero framework code

Exhaustive grep for `managed_repos` across this repo at `393221b` returns exactly one
thing, and it is unrelated:

- `tests/test_memory.py:52,55,62,66` - a memory-index key that happens to share the name

There is no loader, no schema entry, and **no documentation**.
`docs/BUILDING_AGENT_TEAMS.md:129-133` documents `subscribe:` (calling it "rarely
needed") and never mentions `managed_repos`. The framework parses `agent.yaml` and
silently ignores the key.

Its only live consumers are prompts: `<run>/package/roles/director/ROLE.md:273,280`
("A managed repo's Linear binding is a configured subscription", "A managed repo may
explicitly set `tracker: github-issues` in configuration"), plus the decline-on-delete
default, which workers apply by re-reading `managed_repos:` out of
`package/agent.yaml` before touching a remote branch. The core pack's own
`agents/eng-team/roles/director/ROLE.md:216,230` refers to "managed repos" in prose
without ever naming the key.

**So `subscribe:` is enforced in code, at the event server. `managed_repos:` is
enforced by prose.** That asymmetry is the core risk in this change.

### 2.7 Existing precedents to mirror

`MonitorRegistry` (`bobi/monitors/registry.py`) is the closest analogue: pack
defaults (`package/monitors/defaults.yaml`) merged with overrides
(`package/monitors.yaml`, `agent.yaml`), where `enabled: false` acts as a tombstone
that disables a default without editing it (`:66-93`).
Its operator surface is `bobi agent <name> monitors list|add|pause|remove`
(`cli.py:2406-2733`), wired into the agent-scoped group list at `cli.py:3565`.

Name collision to avoid: `bobi/registry.py` is already the **package** registry
(fetch/cache team packages).

---

## 3. Design decisions

### D1. The registry lives in `<run>/workspace/repos.yaml`

Single source of truth for the effective repo set.

Rejected alternatives:

- **`package/agent.yaml` via the unlock hatch** - erased by every reinstall (§2.2).
  This is the option that looks easiest and is actually the bug.
- **`package/repos.yaml`, the monitors pattern** - survives reinstall only by
  accident of the rmtree list, and still requires unlock/relock per operator action.
- **`<run>/state/`** - writable and durable, but that tier is framework-owned runtime
  state (`bubble.json`, cursors, `monitor_state.json`), not operator-authored config.
  Also not what Zach asked for.

Shape, one record per repo, both grants explicit:

```yaml
version: 1
repos:
  - repo: moda-labs/bobi-agent
    subscribed: true
    managed: true
    tracker: github-issues        # or linear:MOD
    source: workspace             # workspace | pack
    note: "onboarded 2026-08-04 by zach"
  - repo: moda-labs/familystories-ai
    subscribed: false
    managed: false
    state: removed                # tombstone, see D2
```

Non-repo topics (`slack:...`, bare `linear:<TEAM>` with no repo binding) stay in
pack `subscribe:`. This registry governs repos, which is what the request is about.
Widening it to all topics is out of scope and noted in §7.

**Resolution is a merge, not a replace, and this is a footgun.** `discover_subscriptions`
returns the pack's explicit list verbatim when non-empty (§2.4). The registry must
therefore resolve to:

```
pack subscribe: entries that are NOT github:<owner>/<repo>   (slack:, linear:<TEAM>, ...)
+ github:<owner>/<repo> for every registry repo with subscribed: true
```

A naive "registry replaces `subscribe:`" would drop
`slack:T0952RZRZ0X:app:...:C0BAEN48KQR` and silently kill every chat channel the
team answers on. The unit tests in §5 must cover exactly this.

### D2. Pack entries are defaults; workspace wins; removals are tombstones

Precedence, resolved per repo:

1. Pack `subscribe:` / `managed_repos:` seed a repo the registry has **never seen**.
2. A workspace record overrides the pack for that repo, in both directions.
3. A workspace record with `state: removed` is a **tombstone**: it survives pack
   upgrades and is never revived by the pack re-declaring the repo.

This answers the upgrade question with a hard no: **a pack change cannot silently
revert a runtime removal.** Two independent mechanisms enforce it - the tombstone,
and `seed_workspace`'s copy-only-if-absent (§2.3).

A pack adding a genuinely new repo does take effect. That is wanted: a pack-declared
repo should still onboard.

This is the `MonitorRegistry` `enabled: false` pattern (§2.7), generalized.

### D3. Operator surface mirrors `monitors`

```
bobi agent <name> repos list                     # effective set + provenance
bobi agent <name> repos show <owner/repo>
bobi agent <name> repos add <owner/repo> [--tracker github-issues|linear:TEAM] [--manage]
bobi agent <name> repos remove <owner/repo>      # writes a tombstone
```

Registered in the agent-scoped group list at `cli.py:3565`, alongside
`monitors`/`workflows`/`roles`.

`repos list` must print **provenance and effective value per repo** (`pack`,
`workspace`, `tombstoned`). The whole point is to make "is this repo managed"
answerable in one command instead of by hand.

It must also surface **subscription drift**: the resolved local set versus what was
last successfully PUT to the deployment, with the timestamp. Per §1.1.1 that drift is
currently invisible on disk, and it is the failure that let a de-subscribed repo keep
delivering for weeks. A `repos list` that shows only local intent would have reported
everything as fine.

**Authorization, stated honestly.** Every `bobi agent ...` command is available to any
worker session on the host, so `--manage` is a privilege-escalation surface: a worker
could grant itself write authority on a new repo.

I am not claiming a hard gate here, because there isn't one available. An agent that
can run the CLI can also set an env var, so an env-var check is a speed bump, not a
boundary, and a TTY check would break legitimate non-interactive operator use. The
realistic mitigations:

- `add` defaults to **subscribe-only**; write authority requires an explicit
  `--manage`. Fail-closed, so the dangerous grant is never the accident.
- Every mutation appends an audit record (actor, timestamp, before/after), so a
  self-grant is visible after the fact rather than silent.
- `repos list` shows `managed` provenance, so a review can spot one.

Whether that is sufficient, or whether `--manage` should be human-only by some stronger
means, is open question §8.3.

### D4. Live re-wiring, honestly scoped

**Events re-wire without a redeploy. Prompt-visible `managed` does not, in phase 1.**

Adding or removing a repo drives the existing PUT (§2.5):

1. CLI writes `workspace/repos.yaml`.
2. A watcher in the manager process polls the file's mtime. The manager already runs
   `MonitorScheduler` as a daemon thread in that same process (`service.py:624-630`)
   and holds the `Session` in-process (`subagent.py:795`), so the watcher reaches the
   live session with no new IPC.
3. On change: recompute topic keys, call `authorize_resources()` for any new
   `github:`/`linear:` topic (§2.5 - skipping this hard-rejects the whole PUT), then
   PUT `{"replace": <new set>}`.

**The new plumbing (§2.5 gap).** Do *not* re-invoke `_start_event_subscription` to do
this: it would redo bubble minting and channel-credential registration, and on failure
its `except` branch unlinks the cursor and re-registers from scratch. Instead add an
`update_subscriptions(keys)` method to the `Subscription` dataclass
(`subagent.py:1186-1198`) that owns the PUT and reassigns `active_subscriptions`, and
have `_resubscribe_on_deaf` read that same state. This is a small, contained change and
it keeps one code path for "what is this session subscribed to" rather than two.

Removal works for free: `replace` is whole-set.

Phase 1 does **not** hot-reload the manager's system prompt. A repo's `managed`
status becomes visible to a *new* worker session immediately (workers read the
registry at launch), but the long-lived director session keeps its boot-time prompt
until it rotates. `Session._rebuild_system_prompt` (`session.py:801`) already exists for
long-term memory; reusing it here is deferred to §7 rather than claimed now.

So the honest statement for the PR and the docs: **adding a repo starts delivering
its events within one poll interval, no redeploy. The director's own prompt reflects
the change on its next rotation.**

### D5. `subscribe` and `managed` stay distinct - not collapsed

Explicitly **not** collapsing them. Collapsing would grant write authority to every
subscribed repo, which today would hand it to `moda-labs/bobi-agent` and
`moda-labs/moda-agents` - the exact repos where the decline-on-delete default was
established after three wrong deletions (§1.2).

Two independent booleans per record: `subscribed` drives topics, `managed` drives
write authority.

The load-bearing follow-through: workers currently read `managed_repos:` out of
`package/agent.yaml`. If this lands without changing that, we create a **third**
source of truth and make the drift worse. So the same change must:

1. Add a queryable framework accessor (`bobi agent <name> repos show <repo> --json`)
   that returns the effective `managed` value.
2. Repoint `roles/director/ROLE.md` and the cleanup/pr-closed prompt guidance at that
   accessor instead of at `package/agent.yaml`.
3. Delete `managed_repos:` from the overlay pack in the same landing sequence, once
   the registry is seeded, so exactly one answer exists.

Framework-generic constraint (`AGENTS.md`, "Keep the framework generic"): `bobi/`
owns the registry, its precedence, and the subscription wiring. The *meaning* of
`managed` (write authority, decline-on-delete) stays a team-pack prompt concern.
The framework's job is to make the bit queryable and authoritative.

### D6. Migration seeds fail-closed, then asks

On first boot after upgrade, seed `workspace/repos.yaml` from the installed pack:
every `github:<owner>/<repo>` in `subscribe:` becomes `subscribed: true`, and
`managed: true` only where a `managed_repos:` entry exists. Everything else
`managed: false`.

That yields, for this fleet:

| Repo | subscribed | managed | Needs a human decision |
|---|---|---|---|
| `underminedsk/lightweave` | true | true | no |
| `moda-labs/moda-skills` | true | true | no |
| `moda-labs/bobi-agent` | true | **false** | **yes** - it is the primary work repo |
| `moda-labs/moda-agents` | true | **false** | **yes** - decline-on-delete default lives here |
| `moda-labs/familystories-ai` | **false** | **false** | **yes** - events still arrive |

Seeding fail-closed reproduces today's *declared* state exactly, so it changes no
behaviour on its own. The three rows above are a genuine question for Zach, not
something this spec should decide silently:

- **`bobi-agent`** - agents work it daily. `managed: true` looks right, and would make
  today's practice match the declaration for the first time.
- **`moda-agents`** - four declines vs three restored deletions. `managed: false`
  (status quo) keeps decline-on-delete as the default. Setting it true reverses a
  standing decision.
- **`familystories-ai`** - deliberately offboarded 2026-08-03 (baohua owns it), yet
  3,155 of its events reached the director's deployment, the last one 50 minutes after
  the pack that excludes it was installed (§1.1.1). The offboarding never took effect
  because nothing re-registered. Seed it as a **tombstone**
  (`subscribed: false, state: removed`), which is the one row where this work has an
  immediate, provable effect: the first registry-driven PUT stops the delivery that
  editing pack source failed to stop.

The `long_term_memory.md` five-repo claim is not fixed by editing that file - it is
sleep-cycle-owned and would drift again. It is fixed by making
`bobi agent <name> repos list` the citable instrument.

---

## 4. Scope

**In:**
- `workspace/repos.yaml` schema, loader, precedence resolution, tombstones
- `bobi agent <name> repos list|show|add|remove`
- Seeding from pack on first boot, fail-closed
- `discover_subscriptions()` reads the registry, pack as fallback/default
- Manager-side watcher driving `authorize_resources` + the existing subscriptions PUT
- `Subscription.update_subscriptions()` (the §2.5 gap)
- Repointing director/cleanup prompts at the accessor; removing overlay `managed_repos:`
- Docs: `docs/BUILDING_AGENT_TEAMS.md:129-133` (the `subscribe:` section), which must
  now describe the registry as the runtime source and the pack list as a default;
  and `docs/EVENT_SERVER.md` (topics and the subscription model)

**Out:**
- Non-repo topics (`slack:`, bare `linear:<TEAM>`) - stay in pack `subscribe:`
- Hot-reloading a running director's system prompt (§7)
- Any change to what `managed` *means* for a worker's behaviour
- Auto-discovering repos from the GitHub org

---

## 5. Verification plan

Framework-generic change (config resolution, event routing, CLI). `CLAUDE.md` names
"event routing" among the brain-agnostic changes "proven by the stub e2e", so no
real-Claude leg is warranted here.

**Unit:**
- Precedence matrix: pack-only, workspace-only, both, tombstoned, tombstone + pack re-add
- **Non-repo topics survive resolution** (D1): a registry with two repos plus a pack
  carrying `slack:` and `linear:MOD` resolves to a set that still contains both.
  This is the test that prevents silently killing every chat channel.
- Reinstall over an edited `workspace/repos.yaml` leaves it byte-identical
  (the `seed_workspace` guarantee, asserted rather than assumed)
- Malformed/absent registry falls back to pack `subscribe:` and logs loudly
- `add` without `--manage` yields `managed: false`

**Integration:**
- `discover_subscriptions()` returns the registry-resolved set
- Registry change triggers `authorize_resources` then a PUT carrying exactly the new
  set; removal produces a `replace` list omitting the repo

**E2E (stub brain, isolated `BOBI_HOME`):**
- Boot a team, `repos add`, assert a webhook for the new repo is delivered **with no
  restart**
- `repos remove`, assert delivery stops
- Reinstall the pack, assert the tombstone holds and the repo does not come back

**Proof of work for the PR:** the e2e transcript showing add -> real delivered event ->
remove -> no delivery, plus `repos list` before and after. This is a CLI/event-routing
change with no UI, so no screenshots apply.

---

## 6. Implementation plan

1. `bobi/repos.py` - schema, load, resolve, tombstones. (Not `registry.py`, taken.)
2. Seeding in the install/boot path, fail-closed.
3. `discover_subscriptions()` reads the registry, pack as default.
4. CLI group + `cli.py:3565` registration.
5. Manager watcher -> `authorize_resources` -> `_start_event_subscription` PUT.
6. `repos show --json` accessor; repoint director/cleanup prompts.
7. Remove overlay `managed_repos:` (separate PR in `moda-labs/moda-agents`, landing
   after this one is deployed, so the registry is seeded before the pack list vanishes).
8. Docs.

Steps 1-4 are independently useful and testable; 5 is the piece that removes the
redeploy; 6-7 are the ones that must not be skipped, or the drift gets worse.

---

## 7. Deferred

- Hot-reloading the director's system prompt on registry change
  (`session.py:802-826` `_rebuild_system_prompt` is the hook).
- Extending the registry to non-repo topics.
- A `bobi doctor` check flagging registry-vs-pack divergence.

---

## 8. Open questions for approval

1. **Two migration rows in D6** - `bobi-agent` and `moda-agents`. Fail-closed seeding
   is safe but leaves `bobi-agent` declared unmanaged while agents work it daily, and
   setting `moda-agents` managed would reverse the standing decline-on-delete default.
   `familystories-ai` is no longer a question: §1.1.1 settles it as a tombstone.
2. **Should the first registry PUT happen automatically on upgrade, or on an explicit
   operator command?** Automatic immediately fixes the live familystories leak. Explicit
   is more predictable but leaves the leak open until someone runs it. My inclination is
   automatic, since seeding reproduces declared intent exactly (D6) and the current state
   is a known-wrong one.
3. **Is fail-closed + audit (D3) enough for `--manage`**, or is host CLI access already
   the authorization boundary and the flag just ergonomics? I could not find a mechanism
   that meaningfully stops an agent that can already run the CLI.

---

## 9. Review record

**Same-model adversarial pass: RUN.** Same model as the author, so it is a
self-critique, not an independent opinion. It found five things, all folded in above
rather than appended:

1. Two **false code citations** in the first draft - `docs/BUILDING_AGENT_TEAMS.md:319`
   for `managed_repos` (that line is about `context/*.md`; the key is not in that file
   at all on `main`), and `install.py:72` for the `agent.yaml` write (it is `:73`). Both
   came from grepping the parked `run/repo` checkout, which sits on an unrelated WIP
   branch, instead of the worktree at `393221b`. Corrected, and the `managed_repos`
   finding got *stronger*: the key is undocumented, not merely unused.
2. Three PUT call sites, not one (§2.5), and the returned `Subscription` handle exposes
   no way to change a live set. Turned a hand-wave into the one concrete piece of new
   plumbing (D4).
3. **Merge-not-replace** (D1): the draft implied the registry replaces `subscribe:`,
   which would have dropped the Slack topic and killed every chat channel. Now an
   explicit resolution rule with a dedicated test.
4. The `--manage` TTY/env gate was **security theatre** - an agent that can run the CLI
   can set an env var. D3 now states what it actually delivers (fail-closed default plus
   audit) and escalates the rest to §8.3.
5. The `familystories-ai` open question was answerable, and answering it produced the
   spec's strongest evidence (§1.1.1).

**Cross-model pass: NOT RUN, still owed.** Verified first-hand in this container, not
assumed: `codex exec` returns `401 Unauthorized: Missing bearer or basic authentication`
against `api.openai.com`, and `aichat` is not installed (`No such file or directory`).
No independent model has read this document.
