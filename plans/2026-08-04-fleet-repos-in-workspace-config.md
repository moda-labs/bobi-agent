# Runtime workspace overlay for agent config

Issue: [#952](https://github.com/moda-labs/bobi-agent/issues/952)
Status: spec, awaiting approval (Gate 1)
Created: 2026-08-04.
Re-validated 2026-08-21 against `main` at `ac2471e6`, and the prose rewritten to
`bobi/prompts/communication_style.md`.
See "2026-08-21 re-validation" for what current code changed.

## Decisions

| | Decision |
|---|---|
| D1 | `<run>/workspace/overlay.yaml` overlays the installed `package/agent.yaml`. |
| D2 | The framework applies exactly one key: `subscribe:`. `bobi/` never learns what a repo is. |
| D3 | An applied key present in the overlay replaces the pack's value for that key. No deep merge. |
| D4 | Presence, not truth. A defined-but-empty `subscribe:` means "nothing", not "auto-detect". |
| D5 | Every top-level key is applied, data, guard, or rejected. A framework key that is parsed but not applied fails boot by name. |
| D6 | The overlay does not go through `Config.load`. |
| D7 | `bobi agent <name> config show` is the read-back: generic, read-only, raw. |
| D8 | The `moda-labs/moda-agents` PR is a blocking co-deliverable. |

Onboarding a repo becomes three lines across two keys in one workspace file,
plus a manager restart.
No pack edit, no PR to `moda-labs/moda-agents`, no rebuild, no reinstall.

## Problem

Onboarding or offboarding a repo means editing agent pack source in a different
repository and redeploying.

The bindings live in `agents/moda-eng-team/agent.yaml` in `moda-labs/moda-agents`,
installed to `run/package/agent.yaml` as a read-only frozen image, in two lists:
`subscribe:` (`package/agent.yaml:117`, routes events) and `managed_repos:`
(`:125`, per-repo `tracker:`, and in practice write authority).

Zach, in Slack `C0BAEN48KQR` thread `1785885345.077239`:

> we should also move away from having to change the agent pack source to have you
> add or remove repos from management. Can you launch a worker to disconnect the
> pack source from this?

> ok, can we manage subscriptions not in the agent pack itself but rather in
> workspace config? Like when we onboard a new repo, it should subscribe to that
> repo's events

Measured cost today: the 2026-08-04 familystories-ai offboard took a commit in a
second repository, a pack rebuild, an install and a restart, 22 minutes from
merge to effect.

## Constraints already ruled

Zach rejected the first design, which proposed a `workspace/repos.yaml` registry
plus a `bobi agent <name> repos add|remove|list` CLI:

> in this design, it looks like pack-specific concepts like repo management have
> leaked into the main bobi agent framework. bobi-agent should NOT have a CLI
> command like `bobi agent <name> repos …`, and if we do have `seed_workspace()`
> it should do it in a general purpose way, not specifically for `managed_repos`

> TBH I'm not that worried about accidentally deleting human branches, they are
> always recoverable. I am more worried about anything that links repos to
> requiring PRs. I think not deleting human branches is more of a policy/prompt thing

Settled, not reopened here:

1. No repo-specific concepts under `bobi/`. No `repos` CLI, no `managed_repos`
   awareness in framework code.
2. `seed_workspace()` stays general purpose. It already is
   (`bobi/install.py:174-192`, copy-only-if-absent), so the action is to change
   nothing.
3. Write authority moves to the workspace with everything else. Keeping it
   pack-side would mean managing a repo still needs a PR to the pack repo.
4. Branch-delete safety is policy and prompt. No manifest hashing, no drift
   detection, no install-time reversion.
5. `bobi agent <name> config show` is approved. The test is whether the *command*
   is domain-specific, not whether the data it prints is. A generic command
   printing repo names is fine; a `repos` command is not. It follows the
   `monitors list` precedent (`bobi/cli.py:2592-2604`).

## The overlay file

`<run>/workspace/overlay.yaml`

```yaml
# Runtime overlay over package/agent.yaml.
# Applied by the runtime: subscribe.  It REPLACES the pack's list, at the next
# manager restart.
# managed_repos, and any other key the framework does not parse, is data for agents.
# A framework key that is NOT applied (roles, services, brain, monitors, build,
# requires, host, auto_dispatch, agent) fails boot by name rather than being ignored.
for_agent: moda-eng-team

subscribe:
  - github:underminedsk/lightweave
  - github:moda-labs/moda-agents
  - github:moda-labs/bobi-agent
  - github:moda-labs/moda-skills
  - slack:T0952RZRZ0X:app:A0BDLA833MW:C0BAEN48KQR
  - linear:MDS
  - linear:MOD

managed_repos:
  - repo: underminedsk/lightweave
    tracker: github-issues
  - repo: moda-labs/moda-skills
    tracker: github-issues
```

The name is `overlay.yaml` because both alternatives are taken.
`paths.global_config_path()` is already `<home>/config.yaml` (`bobi/paths.py:72`),
named in `bobi/config.py:4-5` as a distinct, deliberately-limited concept.
`agent.yaml` is `paths.ROOT_MARKER` (`bobi/paths.py:24`).

Root detection would not actually collide with a second `agent.yaml`:
`resolve_root` states that "The old cwd walk-up is intentionally gone"
(`bobi/paths.py:156`), and `list_agents` (`:131`), `resolve_root_for_agent`
(`:141`) and `resolve_root` (`:153`) are exact-path `is_file()` checks on
`<root>/package/agent.yaml`.
The reason to avoid the name is readability alone.

## Key kinds

Every top-level key in the overlay is exactly one of four kinds:

| Kind | Keys | Who reads it |
|---|---|---|
| Applied | `subscribe` | The framework, at the site named below |
| Data | any key no framework code parses, including `managed_repos` | Agents, and `config show` |
| Guard | `for_agent` | Boot validation only |
| Rejected | a framework-parsed key that is not applied | Nobody: boot fails, by name |

`OVERLAY_APPLIED_KEYS` is a frozenset in `bobi/agent_config.py`, initially
`{"subscribe"}`.
Adding a key later is one entry, not a new mechanism.

`subscribe:` is a framework key: `bobi/events/subscriptions.py` parses it and
`docs/EVENT_SERVER.md` documents it, so naming it in the loader adds no new
vocabulary to `bobi/`.
`managed_repos` is the domain concept, and the framework never parses it at all.

### Why an unapplied framework key is rejected, not logged

A boot log line in a detached daemon's `state/manager.log` is not a signal anyone
sees, and the failure it has to catch is silent.

The director is asked to drop the engineer to a cheaper model.
Following the merge rule as the frozen prompt states it, it writes
`roles: {engineer: {model: claude-sonnet-5}}` into the overlay and restarts.
`roles` is not applied, so `Config.load` goes on parsing the pack's `roles:`
(`package/agent.yaml:113-116`, `model: claude-opus-5`, `effort: max`).
Every engineer keeps running Opus at max effort and nothing fails.
An inert model or credential override is indistinguishable from a working one.

The rejection predicate needs no new inventory.
The framework-parsed set is the union of `Config._parse`'s keys and the raw
readers' keys derived below.
`managed_repos` and `for_agent` pass.
`roles:`, `services:`, `brain:`, `monitors:`, `build:`, `requires:`, `host:`,
`auto_dispatch:` and `agent:` fail the boot that follows the edit, named.

Boot still logs the applied-vs-data line, as a receipt rather than as the
detection.

## Merge rule

An applied key present in the overlay replaces the pack's value for that key (D3).

- It keeps the framework free of domain semantics. Deep merge would need to know
  which keys are sets, which are ordered, and what identity means for a list
  element, which is where repo knowledge would leak in.
- It closes the resurrection trap, given the presence gating below. A pack
  upgrade re-declaring an offboarded repo in `subscribe:` cannot revive it,
  because the pack's `subscribe:` is never consulted once the overlay defines
  that key. No tombstones.
- It is predictable to a human editing YAML.

### Presence gating is a prerequisite (D4)

Replace semantics need presence gating, and today's code gates on truthiness.

`discover_subscriptions` gates on `if explicit:` (`bobi/events/subscriptions.py:61`).
An empty list is falsy, so control falls through to `:68-78`, which calls
`Config.load` and then `detect()`.
`_detect_github` (`bobi/events/adapters.py:57-77`) walks the run root's immediate
children and derives `github:<slug>` from each one's git remote.

So under a naive replace, an overlay `subscribe: []` does not mean "subscribe to
nothing". It means "auto-detect from git remotes", and `slack:` and `linear:`
disappear with it.

Reproduced 2026-08-21 against `main` at `ac2471e6`, in an isolated tree holding
one child repo whose remote is `moda-labs/familystories-ai`, a repo deliberately
offboarded on 2026-08-04:

```
BASELINE (pack subscribe:)  -> ['github:moda-labs/moda-skills', 'linear:MOD']
CASE A   (subscribe: [])    -> ['github:moda-labs/familystories-ai']
CASE B   (subscribe: null)  -> ['github:moda-labs/familystories-ai']
```

That is the resurrection trap the merge rule claims to close, re-armed by the
merge rule itself.

**The contract is therefore presence, not truth.** If the merged document
*defines* `subscribe:`, that value is authoritative even when empty, and the
auto-detect fallback is skipped.

Case B is a different failure and must not be honoured. A `subscribe:` key that
parses to `None` is the mis-indented-list typo, so validation rejects it rather
than reading it as an intentional empty list. `subscribe: []` is honoured.

### Accepted trade-off

A pack upgrade adding a genuinely new entry to an overridden key does not take
effect until an operator copies it across.
Silent pack-side additions to a list the operator now owns are the same class of
surprise as silent resurrection.
The migration therefore copies the complete current `subscribe:` list, including
`slack:` and `linear:`, so the overlay is whole rather than partial.

### Interpolation

Overlay values interpolate `${VAR}` exactly as the pack does.
`explicit_subscriptions` already interpolates whatever `subscribe:` list it is
handed (`bobi/events/subscriptions.py:46-48`), so a literal overlay would need a
second code path.
The loader returns the merged uninterpolated document and each caller
interpolates as it does today.

## Scope

In scope: the merge helper; the one `subscribe:` apply site; atomic write and
loud boot failure, including rejection of an unapplied framework key; a read-only
effective-config view and its CLI attach point; surfacing the ungranted topics a
failed subscription PUT already returns; a commented seed template; migration;
docs; and the companion PR in `moda-labs/moda-agents`.

Out of scope, each with a reason:

- Any `repos` CLI, or any `config set` CLI. Ruled out. The overlay is a YAML file;
  humans and the director edit it.
- Changes to `seed_workspace()`. Already general purpose.
- Routing the overlay through `Config.load`. See below.
- Live reload without a restart. Deferred; the insertion point is named below.
- Machinery for branch-delete safety. Policy and prompt, per ruling 4.
- Promoting `bobi-agent` or `moda-agents` to managed. Migration is
  behaviour-preserving; promotion is a later one-line overlay edit.

## Why not `Config.load` (D6)

`Config.load` is uncached.
`Config.load` (`bobi/config.py:618-623`) calls `_parse` (`:626`), which calls
`_load_yaml` (`:255-258`), which is `yaml.safe_load(path.read_text())`.
There is no `lru_cache` and no memo anywhere in the module.
Every call re-reads from disk.

That has never mattered, because the file it reads cannot change while a process
runs: the installed pack is read-only on disk (`dr-xr-xr-x` on `run/package/`,
`-r--r--r--` on `agent.yaml`) and is replaced only by an install, which is
followed by a restart.
Since #1060, narrowing the write-bit removal to installed team-package images is
the explicit contract of `bobi/runtime_guard.py`, so the frozen image is the one
thing the runtime guard still chmods.

The overlay is operator-writable by construction.
Routing it through `Config.load` flips that invariant for every call site at
once, and every one was written assuming it held.
Three consequences follow:

1. **"Restart to apply" would be false.** An overlay edited at 14:00 with a
   restart planned for 15:00 would be picked up immediately, mid-process, by
   `subagent.py` (`launch_admission`, `:273`), `launch_lineage.py`
   (`max_launch_depth`, `:247`), `monitors/scheduler.py` (`entry_role`, `:469`)
   and every other caller, while `subscribe:` (read once at `service.py:534`)
   stayed on the old set. The fleet would run half-migrated with no signal.

2. **A torn read would degrade live processes silently.** A non-atomic editor
   write, or an agent's `Write` mid-flush, leaves the file briefly invalid. A
   `Config.load` landing in that window raises, and the raise is swallowed into a
   wrong default: `_load_team_config` (`subagent.py:107-115`) returns `None`;
   `supervisor/snapshot.py:55` (`_team_package_version`) yields a null
   `versions.team_package` and `:113` yields empty `expectations.monitors`, so the
   heartbeat publishes with holes rather than failing;
   `supervisor/telemetry.py:101` loses the event-server URL. Boot-time validation
   gives zero coverage, because the edit happens long after boot.

3. **It would expose a surface the spec then has to forbid.** `Config` resolves
   `services:` (credentials), `brain:`, `build:`, `host:` and `requires:`. An
   unsupported-but-reachable surface on the credential and identity path of the
   whole deployment is the wrong shape.

Caching `Config.load` instead was considered and rejected.
Snapshotting the overlay per process buys back only consequence 1.
Consequence 3 remains by construction, consequence 2 shrinks but does not close
(subagents and the supervisor sidecar start at arbitrary times, so their first
read can still land in a torn window), and the cost is a new caching mechanism in
the config loader plus four reader conversions, all to make a surface generic
that the ask does not need generic.

Adding applied keys later is one entry in a frozenset.
Removing `services:` overridability after an operator has used it in production is
a migration.
So the applied set starts at exactly what the ask requires.

## The reader inventory, derived by command

Earlier revisions hand-listed the readers and each list came back incomplete.
This one is derived by command, and the commands are recorded so the next reader
can re-derive rather than trust the table.
Scope: reads of the *installed runtime's* `run/package/agent.yaml`, which is the
file the overlay sits beside.

```bash
# 1. Runtime reads, via the path helper.
grep -rn "agent_yaml_path\|ROOT_MARKER" bobi/ --include=*.py | grep -v "^bobi/paths.py"
# 2. Config.load call sites.
grep -rn "Config\.load(" bobi/ --include=*.py
# 3. Readers that build the path from a string literal, which command 1 misses.
grep -rn '"agent\.yaml"' bobi/ --include=*.py | grep -v "^bobi/paths.py"
```

Command 3 is new this revision.
The published command 1 could not have produced a complete inventory, because a
reader that writes `pack_dir / "agent.yaml"` never matches it.

Command 1 returns 11 hits at `ac2471e6`, all classified:

| Site | What it does with the file | Sees the overlay? |
|---|---|---|
| `bobi/events/subscriptions.py:40` | `explicit_subscriptions` reads `subscribe:`, the ONE parser | **yes, applied** |
| `bobi/config.py:134` | `find_env_var_refs` scans `${VAR}` | no, but see the union below |
| `bobi/config.py:293` | `_project_config_path`, feeds `Config.load` | no, by design |
| `bobi/env.py:47` | reads `brain:` for child env | no, by design |
| `bobi/monitors/script_cache_checks.py:632` | reads `script_cache:` | no, by design |
| `bobi/monitors/registry.py:101` | a source for `_read_records` (`:38-59`), which raw-reads `monitors:` at `:52` | no, by design |
| `bobi/config.py:730`, `:746` | `_parse_build` resolves a sibling `Dockerfile` | not a content read |
| `bobi/doctor.py:321` | `_check_runtime_layout` existence check | not a content read |
| `bobi/subagent.py:1974-1975` | `is_file()` root-marker check | not a content read |

Command 2 returns 43 hits, of which 2 are docstring lines (`build_render.py:255`,
`cli.py:131`), so 41 are real.
They are collapsed into one row because none see the overlay, with one exception:
the `Config.load` fallback inside the apply site itself
(`bobi/events/subscriptions.py:69`).
That fallback is what the merge rule has to reason about, and presence gating is
what keeps it unreached.

Command 3 returns 31 hits, none of which read the installed runtime's file for
content at runtime.
They fall into three groups, and naming them is what keeps the next reader from
re-deriving the same triage:

- Source packs, before installation: `build_render.py`, `compose.py`, `build.py`,
  `registry.py`, `setup/open_mode.py`, `setup/authoring.py`,
  `setup/actions.py:218` (`validate_pack`, `:208`).
- Install-time writes of the image: `install.py:127-129` stamps `agent:` into
  `dest/agent.yaml` before the image is frozen.
- Directory scans: `service.py:266`, `cli.py:593`, `webapp/runtime.py:591`.

### The table is the invariant, and it is a test

The raw readers not seeing the overlay is correct rather than a divergence,
because the overlay cannot set `brain:`, `script_cache:` or `monitors:` in the
first place: such a key is carried as data and applied by nobody.

That is what makes `env.py`'s own warning inapplicable here.
Its docstring (`bobi/env.py:29-32`) says a divergence between it and `Config.load`
"would pass validate yet pin an empty gateway base URL into every child".
A test asserts that `OVERLAY_APPLIED_KEYS` contains no key read by any of those
raw readers, so widening the set later cannot recreate it.

## `find_env_var_refs`: union, not replace

`find_env_var_refs` (`bobi/config.py:126`) drives what `bobi validate` requires.
An overlay `subscribe:` entry carrying `${VAR}` would otherwise not be required,
and a pack `${VAR}` that the overlay replaces away would still be required.

The union goes in `find_env_var_refs` and nowhere lower:

- `_scan_env_refs(agent_yaml: Path)` (`:202`) receives a bare file path and has no
  `project_path`, so it cannot locate the overlay at all.
  `find_env_var_refs(project_path)` is the lowest function that can.
- Forcing it lower would corrupt a live cross-repo contract. `scan_required_vars`
  (`:215`) and `scan_declared_vars` (`:226`) also call `_scan_env_refs`, both
  documented for "a package file that isn't installed yet" (`:203-204`), and
  `scan_declared_vars` "doubles as the prune authority and the env-file filter"
  (`:230-231`). `moda-labs/moda-agents` calls both against *other teams'* source
  `agent.yaml` files at deploy time. Concatenating this runtime's overlay into
  that scan would corrupt another team's declared secret surface.

So `find_env_var_refs` scans the pack, then scans the overlay's text with the same
`_ENV_VAR_RE`, and unions.
`_scan_env_refs`, `scan_required_vars` and `scan_declared_vars` are untouched.
`_build_only_names` (`config.py:156-176`) computes `in_build - elsewhere`, and the
overlay's refs join `elsewhere`.

Union is the safe direction: a var referenced by either file is required.
Over-requiring a secret is an operator annoyance; under-requiring one is an
outage.
This matches `_build_only_names`' own documented stance that "A classification bug
must over-require a secret, never quietly stop requiring one" (`config.py:166-167`).

## Failure handling

### Write side

Every write of the overlay is atomic.
`bobi/fsutil.py` provides `atomic_write_text` (`:100`), `atomic_write_json`
(`:143`) and `file_lock` (`:162`), and `CLAUDE.md` requires durable state to go
through it, so this adopts the house mechanism rather than inventing one.
The migration step writes the overlay with `fsutil.atomic_write_text`.
The framework has no other writer, because there is no `config set` CLI.

The director is not a framework writer and cannot be made one, so the companion
prompt carries the requirement in one line: write a sibling temp file and `mv` it
over the overlay, never edit in place.
This is a known limit, not a guarantee. A human with an editor can still perform a
non-atomic write, and no framework change prevents that.

### Read side

`subscribe:` is reached from five call paths and three are not boot:

| Path | When |
|---|---|
| `discover_subscriptions` from `service.py:534` | boot |
| `discover_subscriptions` from `supervisor/snapshot.py:107` | every heartbeat, in the supervisor process |
| `explicit_subscriptions` from `ingress.py:83`, via `doctor.py:666` | `bobi doctor`, any time |
| `explicit_subscriptions` from `ingress.py:83`, via `service.py:185` | `build_startup_info`, boot |
| `Config.load` fallback at `subscriptions.py:69` | only when presence gating fails |

A post-boot torn read is reachable, and the supervisor process never calls
`_load_config_or_raise` at all, so boot validation alone does not cover it.

**The overlay therefore gets a typed error that no site may swallow into a
default.**
`load_agent_yaml` distinguishes two cases: an *absent* overlay is normal and means
"pack wins", exactly today's behaviour; an overlay that is *present but
unparseable* raises `OverlayError`.

`discover_subscriptions`' bare `except Exception: pass`
(`bobi/events/subscriptions.py:63-66`) must narrow so `OverlayError` propagates.
Leaving it is not an option, because that handler falls through to the
auto-detect fallback.

Verified 2026-08-21 at `ac2471e6`, with an intact pack and a merged read that
raises, which is what a torn overlay does inside the merged loader:

```
torn overlay + intact pack -> ['github:moda-labs/familystories-ai']
```

The same failure presence gating exists to prevent, reached by a different route.
The single-file version of this test is misleading and is why the check has to be
written this way: when the *pack itself* is torn, `Config.load` re-reads the same
broken file at `:69` and raises, so nothing is swallowed. Only the overlay case,
where the pack is intact, reaches auto-detection.

With the narrowing, the supervisor's `except Exception: pass`
(`supervisor/snapshot.py:108-109`) yields the empty list its docstring promises
(`:99-100`) rather than a wrong non-empty one, so the heartbeat reports nothing
instead of reporting fiction.

### Boot validation placement

Boot validation lives in `_load_config_or_raise` (`bobi/service.py:248`), not in
`run_manager_from_config` (`:501`).

The argument is reachability.
`_load_config_or_raise` has three callers, covering every way a manager starts:
`spawn_team` (`:348`, detached start), `start_team` (`:430`, waited start) and
`run_team_foreground` (`:484`, foreground).
The production container path lands on the third, twice over: the entrypoint execs
`bobi agent <name> supervise -- --foreground`
(`docker/docker-entrypoint.sh:637`, `:639`), and the detached spawn itself
re-enters through `bobi agent <name> start --foreground`
(`service.py:383-391`, spawned at `:398`).
`run_manager_from_config:501` is downstream of exactly one of the three.
One placement covers all of them.

Validation checks five things and no more:

1. The document is a mapping.
2. `for_agent` matches the pack's `agent:` (`package/agent.yaml:130`).
3. Each applied key has the shape its consumer expects.
4. An applied key present with a null value is rejected rather than read as empty.
5. No top-level key is a framework-parsed key outside `OVERLAY_APPLIED_KEYS`.

### The `for_agent` guard is required, not optional

`seed_workspace` (`bobi/install.py:174-192`) only adds; nothing removes.
Installing a different team into the same run root would leave a stale overlay
replacing keys in a document that no longer means the same thing.
One line in the overlay and one check at boot turns that from a silent boot
disaster into a named failure.

An optional guard does not guard: the disaster it exists for happens precisely on
a hand-written overlay, so the field would be absent exactly when it is needed.
It is required whenever the overlay defines any applied key.
A seed template that defines no applied keys does not need it, which is why the
template can ship without one.

## Read-back: `bobi agent <name> config show` (D7)

Generic, read-only, per ruling 5.
It prints the effective document with each top-level key marked `pack`, `overlay`,
or `overlay-data`.
`--source` prints the tier column alone; `--json` prints a machine-readable form.

**The output is a contract, because a frozen prompt parses it.**
The companion prompt tells the director to read `managed_repos:` from this
command, so the format is specified here rather than left to the implementation:
keys in the merged document's own order, `--json` emitting
`{"key": {"value": ..., "source": "pack"|"overlay"|"overlay-data"}}`.
One vocabulary for the data tier, `overlay-data`, in both views.

**It prints the raw, uninterpolated document.**
`services:` carries `credentials:` (`package/agent.yaml:10-11`, `:16-18`, `:22-23`),
so an interpolated view would print `GH_TOKEN`, `SLACK_BOT_TOKEN`,
`SLACK_SIGNING_SECRET` and `LINEAR_API_KEY` in plaintext to a terminal and into an
agent transcript.
Uninterpolated, those stay `${GH_TOKEN}`.

**An unparseable overlay makes it exit non-zero with the parse error.**
It must never degrade to a pack-only view.
`config show` is not on the boot path, so it never runs boot validation, and the
director reads write authority from its output: silently falling back to the
pack's `managed_repos` would hand the director a wrong authority list with no
signal.

**The command has to be attached, not just declared.**
`bobi/cli.py` builds the `agent` group from hardcoded lists: single commands at
`:3806-3812`, groups at `:3814-3816`, then `:3822-3827`, which pops the old
top-level alias.
A new `@main.group() def config()` following the `monitors list` precedent is not
reachable as `bobi agent <name> config show` until `"config"` is added to the
group list at `:3814` and to the pop list at `:3822-3826`, which is exactly how
`monitors` appears in both.
`config` is otherwise free: no command or group of that name exists in
`bobi/cli.py`.

## Restart semantics

**Adding a repo takes effect after a manager restart. This is not hot reload.**

That is true by construction: the applied set is `{subscribe}`, and `subscribe:`
is read at `bobi/service.py:534` once, then handed to the spawn (`:668`).
The reconnect path re-asserts an in-memory list rather than re-reading config
(`bobi/events/client.py:420-421`, `_needs_resubscribe`).

### R1: one file edit lands on two clocks

`subscribe:` applies at restart.
`managed_repos` is read by the director through `config show --json` on the next
invocation, per the companion prompt.
So an edit changes authority immediately and event routing at the restart.

The offboard direction is the dangerous one.
An operator removes repo X from both lists at T0.
Authority drops at once; routing persists until the restart.
In that window the manager is still subscribed to X, and `auto_dispatch` still
arms `issue-lifecycle` (`package/agent.yaml:101-104`) and `pr-closed` (`:105-110`)
with `allow_self_authored: true`, so workers can launch against a repo the
director now believes it does not manage, under a branch-delete guard whose scope
is defined by the list that already changed.
The onboard direction is benign: authority arrives before routing does.

The window is bounded by the restart and the mitigation is operational:
**offboard by editing the overlay and restarting immediately, then confirm with
`config show`.**
The docs state this as the offboarding procedure rather than leaving the ordering
to the operator.

A smaller case, for completeness: the supervisor sidecar's `_expectations`
(`bobi/supervisor/snapshot.py:95-109`) calls `discover_subscriptions` on every
heartbeat inside `except Exception: pass`.
It changes no behaviour; it reports what the manager is expected to be subscribed
to, and the read model diffs that against traffic to derive silence.
Between an edit and the restart it reports the intended set while the manager
holds the old one, so a not-yet-subscribed topic can briefly show as silent.

### R2: onboarding needs the grant first, or the restart destroys the cursor

The restart is cheap only when the newly added topic already has its #488 grant.
The happy path is real: the manager session is preserved (cleared only on
`--fresh`), and `_sync_or_reregister` PUTs the newly authorized list against the
existing `deployment_id` (`bobi/subagent.py:1816-1826`), so the cursor survives and
events arriving during the gap replay rather than drop.

Onboarding is precisely the case where the condition fails, because the repo is
new:

1. `authorize_resources` runs with `filter_unauthorized=False`
   (`subagent.py:1807`), so a topic whose grant is denied is kept in the PUT body
   rather than dropped (`bobi/events/server.py:680-682`, `:706-707`).
2. `github:` is a global topic (`event-server/core/src/core.ts:350`), so the server
   demands a matching resource grant and rejects the whole update with a 400 on
   any ungranted topic, deliberately, with no partial write (`core.ts:1441-1443`,
   `unauthorizedGlobalTopics` at `:1836`).
3. `resp.raise_for_status()` raises (`subagent.py:1825`) and the handler falls
   through to `_register_with_retry` (`:1830`).
4. `_register_with_retry` deletes the event cursor itself (`subagent.py:1768-1770`:
   "A fresh deployment starts a fresh seq space") and mints a new
   `deployment_id`/`api_key`.

So adding `github:moda-labs/newrepo` to the overlay and restarting **before** the
GitHub App is installed on that repo does the opposite of what the happy path
promises: the cursor is destroyed and the deployment identity is re-minted,
leaving one `log.info` line in `state/manager.log`.

`_sync_or_reregister` is now the single path for local and remote servers
("the decision is identical for local and remote, so it is made once rather than
mirrored per transport", `subagent.py:1846-1848`), so this does not depend on
which transport the deployment uses.
Its own docstring calls re-registering "the recovery" (`:1794-1795`); the cursor
loss is the part that is not recovery.

**Onboarding therefore has a prerequisite, and the docs state it before the edit:**
install the GitHub App on the repo, or provision the equivalent `linear:`/`slack:`
credential coverage, **first**, then edit the overlay, then restart.

### Q1: no pre-flight grant check exists

There is no command today that reports whether a topic holds its grant, and the
400's body already names the offending topics
(`{"error": "unauthorized_topics", "topics": [...]}`) while `raise_for_status()`
discards them into a generic message.
Making onboarding a one-line edit turns this from a rare deploy-day event into a
routine one, so surfacing that body is in the implementation plan.

### Two operator consequences the docs must state

- A restart can strand in-flight runs. The instruction is "restart, then check the
  runs table and resume stranded runs" (`docs/RUN_RESUME.md`).
- The first restart after onboarding a busy repo is when its backlog can fan into
  auto-dispatch, because `auto_dispatch` (`package/agent.yaml:78`) arms
  `issue-lifecycle` and `pr-closed` with `allow_self_authored: true` for every
  subscribed repo. Expect worker launches on a newly onboarded repo.

### The live-reload insertion point, for a later change

`PUT /deployments/<id>/subscriptions {"replace": [...]}`
(`bobi/subagent.py:1816-1818`, route `event-server/worker/src/index.ts:652`).
`register()` is the wrong primitive: it supersedes the deployment and mints a new
`deployment_id`/`api_key`.
A watcher belongs in the manager process next to `MonitorScheduler`, must reassign
`active_subscriptions` (`subagent.py:1826`) because `_resubscribe_on_deaf` replays
it (`subagent.py:1886-1897`), and must not re-invoke `_sync_or_reregister`, whose
failure branch re-registers and drops the cursor.

## Authority

Write authority moves into the overlay with everything else.
Branch-delete safety is policy and prompt, not machinery: the guard is the role
prompt's decline-on-human-branch default, and nothing here enforces or weakens it.

### R3: the authority list becomes self-modifiable

Ruling 3 is settled and this does not reopen it, but the shape change is real.
`<run>/workspace/` is agent-writable (`drwxr-xr-x bobi bobi`, and agents run as
`bobi`), while `package/agent.yaml` is a read-only frozen image.
Before, adding a repo to `managed_repos` took a reviewed PR to
`moda-labs/moda-agents` plus a reinstall.
After, one unreviewed agent turn writing one line grants it.
The guard is the same policy and prompt layer ruling 4 places the branch-delete
default in.
Accepted under ruling 3, with no mechanical mitigation, deliberately.

## The companion PR is blocking (D8)

`managed_repos` has zero consumers in `bobi/` and zero in the installed pack
beyond its own definition (`run/package/agent.yaml:125`).
Re-verified 2026-08-21.
No role prompt, tool doc, workflow or monitor reads it; the director's belief
about write authority comes from long-term memory.

So moving the list changes nothing on its own.
Until the prompt reads the overlay, there are two lists and the stale one governs.
The `moda-labs/moda-agents` PR carries two changes:

**1. The prompt instruction**, verbatim:

> Repo management lives in `<run>/workspace/overlay.yaml`.
> `subscribe:` there REPLACES the pack's list, and takes effect at the next manager
> restart.
> `managed_repos:`, and any other key the framework does not parse, is data you
> read.
> Do not put `roles:`, `services:`, `brain:`, `monitors:`, `build:`, `requires:`,
> `host:`, `auto_dispatch:` or `agent:` in the overlay: they are not applied, and
> the next boot rejects them by name.
>
> Read `managed_repos:` from the merged view, never from `package/agent.yaml`
> alone:
>
> ```bash
> bobi agent "$(basename "$(dirname "$BOBI_ROOT")")" config show --json \
>   | jq -r '.managed_repos.value[].repo'
> ```
>
> `BOBI_ROOT` is pinned into every agent's environment (`bobi/env.py:168`).
> Do NOT use `basename "$BOBI_ROOT"`: it reads `run` on the canonical
> `<home>/agents/<name>/run` layout and addresses an agent that does not exist.
> The installed agent name is the parent directory, and that is NOT the same
> string as the pack's `agent:` field.
>
> Before adding a repo to `subscribe:`, confirm the GitHub App is installed on it.
> Restarting with an ungranted topic re-mints the deployment and loses the replay
> window.
>
> When you edit the overlay, write a sibling temp file and `mv` it over the target.
> Never edit it in place.

Three things in that text are the ones a reader should check.
The applied-key restriction is stated where the director reads it, not only in
this document.
The name is resolved rather than left as `<name>`: this deployment's installed
name is `eng-team` while its pack's `agent:` is `moda-eng-team`.
The JSON path is `.managed_repos.value`, not `.managed_repos`, because of the
`{"value": ..., "source": ...}` shape.

The shell recipe matches the framework's own rule.
`paths.agent_name_for_root` (`bobi/paths.py:92-94`) returns
`r.parent.name if r.name == "run" else r.name`, and `service.py:388` and `:660`
use it to address the agent.
`$BOBI_AGENT` is not an alternative here: `probe_env` sets it
(`bobi/config.py:323-346`) but that applies to `requires:`/`success:` probes only,
and `bobi/env.py:168` pins `BOBI_ROOT` alone into an agent session.
The explicit "do NOT use `basename $BOBI_ROOT`" line is in the prompt because that
exact mistake caused a total dispatch outage (#1063).

**2. The seed template**, `agents/moda-eng-team/workspace/overlay.yaml`.
The live pack has no `workspace/` directory, so this is a new directory in the
pack, not a file added to an existing one.

## Migration

Write `<run>/workspace/overlay.yaml` once, containing the complete current
`subscribe:` list and the current `managed_repos:` list, copied from
`run/package/agent.yaml` in the pack's own order (lightweave, moda-agents,
bobi-agent, moda-skills), plus `for_agent: moda-eng-team`.
Behaviour on day one is unchanged.
Consumption is order-insensitive (`bobi/service.py:543-545`, `:549-551` are
membership tests), so this is a semantic no-op rather than a literal byte match.

`moda-labs/familystories-ai` appears in neither list.
It was offboarded 2026-08-04 22:24 UTC (`c0ec10a` in moda-agents) because baohua
is its sole engineering team.
Verified genuinely off: parsing the director's event log by
`payload.repository.full_name` over a 33-hour window shows 628 familystories
events, newest `2026-08-04T21:29:36`, and none after the 22:37:54 pack reinstall.
A substring grep for `familystories` inflates this badly, because the string
appears inside `moda-labs/bobi-agent` issue and PR payloads.

The commented seed template ships in the pack's new `workspace/`.
It is safe because it defines no keys, and it is how an operator discovers that
the file and the merge rule exist.
Its comments carry the applied-key list, so the operator is told at the point of
editing that `services:` will not take effect.
`seed_workspace()` copies it only if absent and is otherwise untouched.

The pack's `subscribe:` and `managed_repos:` stay in place as the seed layer for a
fresh deployment that has no overlay yet.

### R4: the fleet trade

For one deployment this is a clear win: 22 minutes and a cross-repo PR become one
file edit and a restart.
For N deployments it replaces a PR-reviewed, atomic, version-controlled change
with N hand edits on N durable volumes (`Dockerfile` `VOLUME ["/data"]`), with no
drift detection and no audit trail.
That is the deliberate cost of the ask, it is accepted, and the mitigation if the
fleet grows is to converge the overlays from a source of record rather than to
re-pack.

## Verification plan

Merge and key kinds:

- An overlay key in the applied set replaces the pack's key; absent keys fall
  through; an absent overlay is exactly today's behaviour.
- An overlay key outside the applied set that no framework code parses
  (`managed_repos`) is carried as data, changes no runtime behaviour, and is named
  in boot's applied-vs-data log line.
- An overlay key outside the applied set that the framework does parse fails boot
  with the key named. Parametrized over `roles`, `services`, `brain`, `monitors`,
  `build`, `requires`, `host`, `auto_dispatch` and `agent`, so the case the
  director actually hits (`roles:` for a cheaper model, against a pack that sets
  `claude-opus-5`) is covered rather than assumed.
- The predicate behind that rejection: every key in `Config._parse` and every key
  the raw readers read is either in `OVERLAY_APPLIED_KEYS` or rejected. This is
  what stops the two sets drifting apart as `Config` gains fields.
- `OVERLAY_APPLIED_KEYS` contains no key read by `env.py:47`,
  `script_cache_checks.py:632`, `registry.py:101` or `Config.load`. `registry.py`
  is in that list because `_read_records` (`:38-59`) is a raw
  `yaml.safe_load(...).get("monitors")` at `:52`, so adding `monitors` to the
  applied set would diverge three readers at once: `MonitorRegistry.load`,
  `Config._parse`'s uninterpolated `monitors_raw`, and the overlay.
- `${VAR}` in an overlay `subscribe:` entry interpolates identically to the pack.
- `find_required_env_vars` unions pack and overlay refs. An overlay `${VAR}`
  becomes required; a pack `${VAR}` the overlay replaced away stays required.

Presence gating and failure:

- A manager booted with an overlay `subscribe: []` subscribes to nothing and does
  not fall through to git-remote auto-detection. Without presence gating it
  returns `github:<whatever run/ happens to contain>`.
- An overlay `subscribe:` with a null value (the mis-indented-list typo) fails
  boot, and is not read as an intentional empty list.
- `OverlayError` from a torn overlay propagates through
  `subscriptions.py:63-66` rather than being swallowed into the auto-detect
  fallback, and the supervisor's handler yields the empty list its docstring
  promises. **The test must use an intact pack and a torn overlay.** With a torn
  pack the fallback re-reads the same broken file at `:69` and raises on its own,
  so the test passes without the fix and proves nothing.
- A malformed overlay fails manager boot loudly with the file and the reason, at
  `_load_config_or_raise` (`bobi/service.py:248`).
- `for_agent` mismatching the pack's `agent:` fails boot with a named error.
- Regression, the resurrection trap: pack `subscribe:` re-declaring an offboarded
  repo does not resubscribe it while the overlay defines `subscribe:`.
- Regression: `explicit_subscriptions` is the only `subscribe:` parser, so its two
  callers cannot disagree on an empty, an absent, or a populated value. This is a
  guard on the consolidation `main` already made, not a reconciliation.

CLI and prompt contract:

- `config show --json` emits the specified shape, and `managed_repos` resolves from
  the overlay. The frozen prompt parses this output, so the format is under test.
- `bobi agent <name> config show` resolves through the CLI's runner, not merely as
  a declared group. A decorator alone leaves it unreachable until `"config"` joins
  the lists at `cli.py:3814` and `:3822-3826`.
- The prompt's own invocation works end to end: the shell recipe agrees with
  `paths.agent_name_for_root` on the canonical layout, and `.managed_repos.value`
  is the path that returns the repo list. The prompt is frozen text against a
  machine-readable contract; nothing else tests that the two agree.

Integration:

- A full manager start against the local event server registers exactly the
  overlay's `subscribe:` set, and a repo removed from the overlay is genuinely
  unsubscribed after restart rather than merely absent from the file.
- Framework purity, stated as a predicate because the loose version is untestable:
  the literal strings `managed_repos` and `OVERLAY_APPLIED_KEYS`' forbidden members
  do not appear in `bobi/`. (`tracker` already appears at `setup/services.py` and
  in `events/drain.py`'s `_AckWatermark`, and `repo` appears in 20+ modules, so the
  predicate names strings rather than concepts.)
- `seed_workspace()` unchanged; the seed template defines no keys.
- Blocker 1 falsified, at a `Config.load` consumer. Asserting that live
  *subscriptions* do not change mid-process is vacuous: `subscribe:` is captured
  into a local at `service.py:534` and handed to the spawn at `:668` under both
  designs. The assertion has to land on a `Config.load` consumer, which the
  chokepoint design would have made hot: edit the overlay while a manager runs and
  assert `launch_admission` (`subagent.py:273`), `max_launch_depth`
  (`launch_lineage.py:247`) and `entry_role` (`monitors/scheduler.py:469`) still
  resolve to their pack values.
- A torn overlay present at boot fails the boot with a named error rather than
  starting on pack subscriptions.
- The #488 resource-grant path authorizes overlay-sourced topics on the restart
  path. Asserting that "an ungranted topic is reported rather than silently
  dropped" describes the `filter_unauthorized=True` branch, which the restart path
  never takes: it passes `filter_unauthorized=False` (`subagent.py:1807`), keeps
  the ungranted topic, and the server 400s the whole PUT. So the assertion is on
  the consequence: a manager restarted with an ungranted overlay `subscribe:` entry
  loses its event cursor and re-mints its deployment, and the operator-visible log
  names the ungranted topics rather than reporting a bare 400.

## Implementation plan

| | Step |
|---|---|
| A1 | `bobi/agent_config.py`: `load_agent_yaml(project_path) -> dict` returning the merged uninterpolated document, plus `OVERLAY_APPLIED_KEYS` and the validation entry point. |
| A2 | Apply it at `explicit_subscriptions` (`bobi/events/subscriptions.py:25-48`), gating on presence rather than truthiness, so a defined-but-empty `subscribe:` means "nothing". Narrow `discover_subscriptions`' swallow (`:63-66`) so `OverlayError` propagates. No other reader changes. |
| A3 | Boot validation in `_load_config_or_raise` (`bobi/service.py:248`), which covers all three start paths (`:348`, `:430`, `:484`). Five checks, plus the applied-vs-data log line for the keys that pass. |
| A4 | `find_env_var_refs` (`config.py:126`) unions the overlay's `${VAR}` refs with the pack's, and `_build_only_names` folds overlay refs into `elsewhere`. `_scan_env_refs`, `scan_required_vars` and `scan_declared_vars` are untouched. |
| A5 | `bobi agent <name> config show`, with `--source` and `--json`, following `monitors list` (`bobi/cli.py:2592-2604`), and attached: `"config"` joins the group list at `cli.py:3814` and the pop list at `:3822-3826`. |
| A6 | Surface the ungranted topics on a failed subscription PUT. The server returns them in the 400 body (`core.ts:1443`); `_sync_or_reregister` (`subagent.py:1828-1830`) currently logs only `raise_for_status()`'s generic message, at `info`, before re-registering and discarding the cursor. |
| A7 | Migration overlay written into the live runtime with `fsutil.atomic_write_text`. |
| A8 | Docs: `docs/BUILDING_AGENT_TEAMS.md` gains a runtime-overlay section covering the merge rule and its three outcomes, the restart consequences, the grant prerequisite before an onboarding edit, the two-clock offboarding procedure, the supervisor-heartbeat exception, and one more consequence of the env-ref union: an overlay referencing an unset `${VAR}` fails the whole manager boot at `_validate_or_raise` (`service.py:271`, called at `:488`), not just the subscription. `managed_repos` does not appear in `docs/`, so there is nothing to rewrite. |
| A9 | Blocking co-deliverable: the `moda-labs/moda-agents` PR carrying both the prompt text and the `workspace/overlay.yaml` seed template. |

Complexity is small.
The previous revision's size came from routing every `Config.load` site and six
raw readers through one loader; scoping the applied set to `subscribe:` removes
that work rather than deferring it.

## 2026-08-21 re-validation

The spec was written 2026-08-04 against `2b8d7cb9`.
`main` gained 82 commits by `ac2471e6`, so every premise and citation was checked
first-hand against that tree.

**The design survives, and it got smaller.**
No blocker was refuted, no decision reversed.
Two things `main` did on its own removed work this spec was going to do.

| | Finding | Effect on the spec |
|---|---|---|
| F1 | #1000 made `explicit_subscriptions` the ONE parser for `subscribe:` (D078, `subscriptions.py:25-48`). `ingress.py` no longer opens `agent.yaml`; it calls that parser (`:78`, `:83`). | Two apply sites become one. Step A2 shrinks. The round-5 finding that the two sites disagree is fixed upstream; its test survives only as a guard. |
| F2 | The local-server and hosted registration branches were consolidated into one `_sync_or_reregister` (`subagent.py:1786-1830`, comment at `:1846-1848`). | R2's mechanism no longer depends on transport, and the round-5 leg-C argument about which branch unlinks the cursor is moot. The failure log is `log.info`, quieter than the WARNING the spec claimed. |
| F3 | The spec's published derivation command could not produce a complete inventory: a reader that writes `pack_dir / "agent.yaml"` never matches `agent_yaml_path\|ROOT_MARKER`. | A third grep is added and its 31 hits are classified. `setup/actions.py` is reclassified: `validate_pack` (`:208-226`) inspects an authored source pack, not the installed runtime. |
| F4 | `Config.load` has 41 real call sites at `ac2471e6` (43 grep hits, 2 docstrings), not the 40 the spec stated. | The count is restated with the command and the SHA. No decision depends on the number; the argument is "all of them". |
| F5 | #1068 landed `paths.agent_name` and `probe_env` (`config.py:323-346`) after `basename "$BOBI_ROOT"` caused a total dispatch outage (#1063). | The prompt's `basename "$(dirname ...)"` recipe was already correct and now matches a framework rule it can cite (`paths.py:92-94`). An explicit "do NOT use `basename $BOBI_ROOT`" line is added. `$BOBI_AGENT` is not an alternative: `env.py:168` pins `BOBI_ROOT` alone into an agent session. |
| F6 | #1060 narrowed chmod write-bit removal to installed team-package images. | Strengthens D6: the frozen pack image is now the one thing `runtime_guard` chmods, which is exactly the invariant routing the overlay through `Config.load` would break. |

Re-verified by execution, not by reading:

- The truthiness fall-through, in an isolated tree. `subscribe: []` and
  `subscribe:` both yield `['github:moda-labs/familystories-ai']`.
- The torn-overlay swallow, with an intact pack and a raising merged read, yields
  the same. This run also showed why the single-file version of the test is
  misleading, which is now written into the verification plan.
- `Config.load` is still uncached: no `lru_cache`, no memo in `bobi/config.py`.
- `managed_repos` still has zero consumers in `bobi/` and zero in the installed
  pack beyond `agent.yaml:125`.
- The pack image is still `dr-xr-xr-x` / `-r--r--r--`; `<run>/workspace/` is still
  `drwxr-xr-x bobi bobi`.
- `config` is still a free name in `bobi/cli.py`.

Citations moved in `config.py`, `cli.py`, `paths.py`, `subagent.py`,
`supervisor/snapshot.py`, `doctor.py`, `env.py`, `monitors/registry.py`,
`events/server.py`, `install.py`, `core.ts` and `docker-entrypoint.sh`, and are
updated throughout.
`bobi/service.py` and `package/agent.yaml` are the only cited files whose line
numbers all survived.
The `doctor.py:253` paragraph is deleted: it withdrew a claim about
`_check_runtime_layout`, and `:253` is now unrelated code (`#1063` requires
reporting). The layout check is `doctor.py:321`.

## Withdrawn claims

Kept as a list so a reviewer who read an earlier revision can see what is gone.

| Claim | Status |
|---|---|
| "Onboarding is two lines in one file" | Wrong by the spec's own example. It is three lines across two keys. |
| Runtime root detection "walks for that filename", so `agent.yaml` would collide | False. `resolve_root` is an exact-path check (`paths.py:156`). The reason to avoid the name is readability. |
| `subscribe:` is read "at exactly two sites, both during manager startup" | False. Five call paths, three not boot, and the supervisor never runs boot validation. |
| Blocker 2 shrinks "to two boot-time reads where validation actually works" | False, same reason. What closes it is `OverlayError` plus narrowing the swallow. |
| The bare `except` can stay because the overlay is "already parsed at boot" | Circular. Replaced by the five call paths. |
| Validation belongs at `run_manager_from_config` (`service.py:501`), because `service.py:484` "would have failed earlier" | Circular. Replaced by three-caller reachability. |
| The env-ref union belongs in `_scan_env_refs` | Impossible (no `project_path`) and harmful (corrupts a cross-repo contract). Moved to `find_env_var_refs`. |
| "The overlay can never carry `build:`" | False. The conclusion survives on the safe-direction argument alone. |
| The supervisor heartbeat is "the only place the two clocks can disagree" | False. See R1. |
| `registry.py` is "not a content read" | False. `_read_records` raw-reads `monitors:`. It joins the invariant test. |
| `doctor.py:253` "keeps reporting pack-only truth" | False then (it was a layout check) and the line is now unrelated. Paragraph deleted. |
| "Events replay rather than drop" on any restart | False in the onboarding case. See R2. |
| The blocker-1 falsifier test as first written | Vacuous: passed under both designs. Retargeted at `Config.load` consumers. |
| `for_agent` is optional ("if present") | An optional guard does not guard. Required whenever the overlay defines an applied key. |
| 42, then 41, then 40 `Config.load` call sites | 41 at `ac2471e6`. Derive it, do not quote it. |
| The cursor loss depends on which register branch runs | Moot. One branch now (F2). |

## Review record

Rounds 1 and 2 ran against the pre-ruling `repos.yaml` design and returned five
blockers before Zach's premise ruling replaced it.
Round 3 produced the generic-merge revision.
Round 4 returned four blockers, all rooted in making `Config` the chokepoint, and
that decision is reversed rather than patched.

Round 5 reviewed the reversal in three legs.
Legs A and B returned; **leg C was still running when its launcher exited**, so its
findings reached nobody until they were recovered from its transcript.
All three are committed verbatim under
[`plans/reviews/`](https://github.com/moda-labs/bobi-agent/tree/agent/953/plans/reviews)
rather than summarized.

| Round-5 finding | Disposition |
|---|---|
| Empty `subscribe:` falls through to git-remote auto-detection | Accepted. Presence gating (D4), now part of the merge rule and A2. |
| Torn overlay swallowed into that same fallback; the supervisor re-reads post-boot | Accepted. `OverlayError`, and the swallow must narrow. |
| Two clocks: `subscribe:` at restart, `managed_repos` immediately | Accepted. R1. |
| The env-ref union cannot live in `_scan_env_refs` | Accepted. Moved to `find_env_var_refs`. |
| The "blocker 1 falsified" test is vacuous under both designs | Accepted. Retargeted at `Config.load` consumers. |
| `config show` had no failure mode and no redaction statement | Accepted. Raw and uninterpolated, non-zero exit on an unparseable overlay. |
| `registry.py` misclassified as "not a content read" | Accepted. It joins the invariant test. |
| `workspace/` is agent-writable, so the authority list is self-modifiable | Accepted as a stated consequence under ruling 3. R3. |
| Leg C: the frozen prompt states a universal merge rule the design does not implement, so `roles:`/`services:`/`brain:`/`monitors:` are silently inert | Accepted. Boot rejects a framework-parsed key outside the applied set (D5), and the prompt carries the restriction. |
| Leg C: "events replay rather than drop" is false when the PUT 400s on a missing #488 grant | Accepted. R2. |
| Leg C: `config show` is unreachable until `"config"` joins the CLI attach lists, and the prompt has no `<name>` resolution and no JSON path | Accepted. Attach point named; the prompt resolves the name and reads `.managed_repos.value`. |
| Leg C: the "unreachable placement" argument for `_load_config_or_raise` is circular | Accepted. Replaced with three-caller coverage. |
| Leg C: the seed template's adjacent lines contradict each other | Partly refuted. The next line already restricted the rule. The template is rewritten anyway, because the frozen prompt had no restriction at all. |
| Leg C B1 (torn read moves to `snapshot.py`), M1, M2, MINOR 2, MINOR 3 | Already addressed in the A/B fold; not re-applied. |

**Spec review gate.**
The house binding is `/gstack-plan-eng-review`, `/gstack-plan-design-review` and
`/gstack-plan-ceo-review`.
Those skills are interactive and cannot run in a headless worker, so the three
lenses ran as adversarial passes instead: architecture and edge cases and tests,
design and operator experience, and scope.
A labelled substitution, not a claim to have run the skills.
Not plan-born, so all three lenses apply.

**Q2: the cross-model pass is still owed.**
`codex exec` returns `401 Unauthorized: Missing bearer or basic authentication` in
this container, `aichat` has no provider config, and there is no fallback LLM key.
Re-checked 2026-08-21, unchanged.
Same-model passes are a fallback, not a substitute, so this is recorded as an
outstanding debt rather than waived.
