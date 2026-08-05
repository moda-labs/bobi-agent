# Runtime workspace overlay for agent config

Issue: [#952](https://github.com/moda-labs/bobi-agent/issues/952)
Status: spec, awaiting approval (Gate 1)
Created: 2026-08-04.
Rewritten 2026-08-05 under Zach's premise constraints.
Revised 2026-08-05 (round 4): the chokepoint decision is reversed, see "Why not `Config.load`".
Revised 2026-08-05 (round 5): presence gating, `OverlayError`, and the two-clock consequence.
Revised 2026-08-05 (round 5, leg C recovered): rejection of unapplied framework keys, the
grant prerequisite on restart, and the `config show` attach point.

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

Measured cost today: yesterday's familystories-ai offboard took a commit in a second
repository, a pack rebuild, an install and a restart, 22 minutes from merge to effect.

### The constraints that killed the first design

This is a rewrite.
The first version proposed a `workspace/repos.yaml` registry plus a
`bobi agent <name> repos add|remove|list` CLI.
Zach rejected the premise:

> in this design, it looks like pack-specific concepts like repo management have
> leaked into the main bobi agent framework. bobi-agent should NOT have a CLI
> command like `bobi agent <name> repos …`, and if we do have `seed_workspace()`
> it should do it in a general purpose way, not specifically for `managed_repos`

> TBH I'm not that worried about accidentally deleting human branches, they are
> always recoverable. I am more worried about anything that links repos to
> requiring PRs. I think not deleting human branches is more of a policy/prompt thing

Binding rulings, settled, not reopened here:

1. **No repo-specific concepts under `bobi/`.**
   No `repos` CLI, no `managed_repos` awareness in framework code.
2. **`seed_workspace()` stays general purpose.**
   Verified it already is (`bobi/install.py:118-136`, copy-only-if-absent), so the
   correct action is to change nothing.
3. **Write authority moves to the workspace with everything else.**
   Keeping it pack-side would mean managing a repo still needs a PR to the pack repo.
4. **Branch-delete safety is policy/prompt.**
   No machinery: no manifest hashing, no drift detection, no install-time reversion.
5. **`bobi agent <name> config show` is approved.**
   Zach's test: what matters is whether the *command* is domain-specific, not whether
   the data it prints is.
   A generic command printing repo names is fine; a `repos` command is not.
   It follows the `monitors list` precedent (`bobi/cli.py:2417-2419`).

## Solution

Add one generic primitive: **a runtime workspace overlay over the installed
`agent.yaml`.**

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

Onboarding becomes: add two lines to one workspace file, restart the manager.
No pack edit, no PR to `moda-labs/moda-agents`, no rebuild, no reinstall.

**Filename.**
`overlay.yaml`, not `config.yaml` and not `agent.yaml`.
`paths.global_config_path()` is already `<home>/config.yaml` (`bobi/paths.py:71-72`),
named in `bobi/config.py:4-5` as a distinct, deliberately-limited concept, so
`config.yaml` is taken.
`agent.yaml` is `paths.ROOT_MARKER` (`bobi/paths.py:23`).

*Correction to the previous revision.*
It justified avoiding `agent.yaml` by claiming runtime-root detection "walks for that
filename".
That is false and is withdrawn.
`resolve_root` states outright that "The old cwd walk-up is intentionally gone"
(`bobi/paths.py:123-125`); `list_agents` (`:106`), `resolve_root_for_agent` (`:112`)
and `resolve_root` (`:132`, `:140`) are all exact-path `is_file()` checks on
`<root>/package/agent.yaml`.
A file at `<run>/workspace/agent.yaml` would collide with nothing mechanically.
The remaining reason to avoid the name is readability, and that is a weaker reason
than the `config.yaml` collision, so it is not the one the decision rests on.

### What the runtime applies, and what is only data

Every top-level key in the overlay lands in exactly one of four kinds, and that
classification is the design:

| Kind | Keys | Who reads it |
|---|---|---|
| **Applied** | `subscribe` | The framework, at the sites named below |
| **Data** | any key no framework code parses, including `managed_repos` | Agents, and `config show` |
| **Guard** | `for_agent` | Boot validation only |
| **Rejected** | a framework-parsed key that is not applied | Nobody: boot fails, by name |

`OVERLAY_APPLIED_KEYS` is a frozenset in `bobi/agent_config.py`, initially
`{"subscribe"}`.
Adding a key later is one entry, not a new mechanism.

`subscribe:` is a **framework** key: `bobi/events/subscriptions.py` parses it,
`bobi/ingress.py` parses it, `docs/EVENT_SERVER.md` documents it.
Naming it in the loader adds no new vocabulary to `bobi/`.
`managed_repos` is the domain concept, and under this design the framework never
parses it at all, which satisfies ruling 1 more completely than a generic merge that
carries it through `Config`.

**An unapplied framework key is rejected, not logged.**
Round 5 leg C killed the previous answer, which was a boot log line, and it was right:
a line in a detached daemon's `state/manager.log` is not a signal anyone sees.
The failure it has to catch is concrete.
The director is asked to drop the engineer to a cheaper model.
Following the merge rule as the frozen prompt states it, it writes
`roles: {engineer: {model: claude-sonnet-5}}` into the overlay and restarts.
`roles` is not applied, so `Config.load` goes on parsing the pack's `roles:`
(`package/agent.yaml:113-116`, `model: claude-opus-5`, `effort: max`).
Every engineer keeps running Opus at max effort and nothing fails.
An inert model or credential override is indistinguishable from a working one until
something else breaks.

**So boot rejects a top-level overlay key that the framework parses but does not
apply.**
The predicate needs no new inventory, because this spec already derived it: the
framework-parsed set is the union of `Config._parse`'s keys and the four raw readers'
keys (`brain:` at `env.py:34`, `agent:` at `setup/actions.py:164`, `script_cache:` at
`script_cache_checks.py:649`, `monitors:` at `registry.py:80`).

- `managed_repos`, and any future key like it, is parsed by nothing in `bobi/`, so it
  passes as data.
  `for_agent` passes too, as the guard validation consumes itself.
- `roles:`, `services:`, `brain:`, `monitors:`, `build:`, `requires:`, `host:`,
  `auto_dispatch:` and `agent:` fail the boot that follows the edit, with the key
  named.

This is the cheap half of the reason the applied set can stay at `{subscribe}`.
Scoping made an unsupported override *inert*; rejecting it makes the operator's
mistake *visible*, which is what "unsupported" has to mean on a credential and
identity surface.
Boot still logs the applied-vs-data line, but it is now a receipt rather than the only
detection.

### Merge rule: replace, not deep-merge

**An applied key present in the overlay replaces the pack's value for that key.**

The other two outcomes are settled by the rejection rule above, and the three together
are exhaustive: a key nothing in `bobi/` parses is carried as data, and a
framework-parsed key outside the applied set fails boot.
There is no fourth case in which an overlay key quietly does nothing.

- It keeps the framework free of domain semantics.
  Deep-merge would need to know which keys are sets, which are ordered, and what
  identity means for a list element, which is exactly where repo knowledge would leak in.
- It closes the resurrection trap, **given the presence gating specified next**.
  A pack upgrade re-declaring an offboarded repo in `subscribe:` cannot revive it,
  because the pack's `subscribe:` is never consulted once the overlay defines that key.
  No tombstones, no second mechanism.
  Round 4 called this "by construction"; round 5 showed the construction was missing a
  piece, and that piece is a prerequisite rather than a refinement.
- It is predictable to a human editing YAML.

**Replace semantics require presence gating, and today's code gates on truthiness.**
This is the sharpest thing round 5 found and it is a prerequisite, not a detail.

`discover_subscriptions` gates on `if explicit:` (`bobi/events/subscriptions.py:42`).
An empty list is falsy, so control falls through to `:47-57`, which calls
`Config.load` and then `detect()`.
`_detect_github` (`bobi/events/adapters.py:59-77`) walks the run root's immediate
children and derives `github:<slug>` from each one's git remote.

So under a naive replace, an overlay `subscribe: []` does not mean "subscribe to
nothing".
It means "auto-detect from git remotes", and `slack:` and `linear:` disappear with it.
Reproduced against the live tree by injecting the merged value at the read site: a
pack `subscribe:` yields `['github:moda-labs/moda-skills', 'linear:MOD']`, while a
merged-empty `subscribe:` yields `['github:moda-labs/familystories-ai']`, a repo
deliberately offboarded on 2026-08-04.
That is the resurrection trap the merge rule claims to close, re-armed by the merge
rule itself.

**Therefore the contract is presence, not truth:** if the merged document *defines*
`subscribe:`, that value is authoritative even when empty, and the auto-detect
fallback is skipped.
Both apply sites must use the same rule.
They do not today: `ingress.py:75-79` returns `[]` with no fallback while
`subscriptions.py:42` falls through, so the same overlay currently yields different
answers at the two sites.
Making them agree is part of step 2, not a follow-on.

A `subscribe:` key that parses to `None` (the mis-indented-list typo) is a *different*
case and must fail boot rather than be read as an intentional empty list.
Validation distinguishes the two: `subscribe: []` is honoured, `subscribe:` with a
null value is rejected.

**Trade-off, accepted deliberately.**
A pack upgrade adding a genuinely new entry to an overridden key does not take effect
until an operator copies it across.
Silent pack-side additions to a list the operator now owns are the same class of
surprise as silent resurrection.
The migration therefore copies the *complete* current `subscribe:` list, including
`slack:` and `linear:`, so the overlay is whole rather than partial.

**Interpolation:** overlay values interpolate `${VAR}` exactly as the pack does.
`discover_subscriptions` already interpolates whatever `subscribe:` list it is handed
(`bobi/events/subscriptions.py:38-40`), so a literal overlay would need a second code
path.
The loader returns the merged **uninterpolated** document and each caller interpolates
as it does today.

## Scope

In scope: the merge helper; the two `subscribe:` apply sites; atomic-write and
loud-boot-failure handling, including rejection of an unapplied framework key; a
read-only effective-config view and its CLI attach point; surfacing the ungranted
topics a failed subscription PUT already returns; a commented seed template;
migration; docs; and the companion PR in `moda-labs/moda-agents`.

Out of scope, each with a reason:

- **Any `repos` CLI, or any `config set` CLI.**
  Ruled out.
  The overlay is a YAML file; humans and the director edit it.
- **Changes to `seed_workspace()`.**
  Already general purpose.
- **Routing the overlay through `Config.load`.**
  Reversed this round.
  Reasons below.
- **Live reload without a restart.**
  Deferred; correct insertion point named below.
- **Machinery for branch-delete safety.**
  Policy/prompt, per ruling 4.
- **Promoting `bobi-agent` or `moda-agents` to managed.**
  Migration is behaviour-preserving.
  Promotion is a later one-line overlay edit and is not this change.

## Technical approach

### Why not `Config.load`, reversed from the previous revision

The previous revision made `Config.load` the chokepoint so the merge would reach all
its call sites "for free".
That is withdrawn.
The decisive fact, verified first-hand this round:

**`Config.load` is uncached.**
`Config.load` (`bobi/config.py:551-556`) calls `_parse`, which calls `_load_yaml`
(`:243-246`), which is a bare `path.read_text()`.
There is no `lru_cache`, no memo, nothing else in the module.
Every call re-reads from disk.

That has never mattered, because the file it reads cannot change while a process runs:
the installed pack is read-only on disk (`dr-xr-xr-x` on `run/package/`, `-r--r--r--`
on `agent.yaml`) and is replaced only by an install, which is followed by a restart.
**The overlay is operator-writable by construction.**
Routing it through `Config.load` flips that invariant for all 40 call sites at once,
and every one of them was written assuming it held.

Three consequences follow, and they are what invalidated the previous revision:

1. **"Restart to apply" would be false.**
   An overlay edited at 14:00 with a restart planned for 15:00 would be picked up
   immediately, mid-process, by `subagent.py` (`launch_admission`),
   `launch_lineage.py` (`max_launch_depth`), `monitors/scheduler.py` (`entry_role`)
   and every other `Config.load` caller, while `subscribe:` (read once at
   `service.py:534`) stayed on the old set.
   The fleet would run half-migrated with no signal.

2. **A torn read would degrade live processes silently.**
   A non-atomic editor write, or an agent's `Write` mid-flush, leaves the file briefly
   invalid.
   A `Config.load` landing in that window raises, and the raise is swallowed into a
   wrong default: `subagent.py:106-113` returns `None`; `supervisor/snapshot.py:48`
   (`_team_package_version`) yields a null `versions.team_package` and `:105` (inside
   `_expectations`) yields an empty `expectations.monitors`, so the heartbeat still
   publishes, with holes rather than as a whole; `supervisor/telemetry.py:96` loses
   the event-server URL.
   Boot-time validation gives zero coverage, because the edit happens long after boot.

3. **It would expose a surface the spec then had to forbid.**
   `Config` resolves `services:` (credentials), `brain:`, `build:`, `host:` and
   `requires:`.
   The previous revision's own docs step told operators that replacing those
   "is unsupported".
   An unsupported-but-reachable surface on the credential and identity path of the
   whole deployment is the wrong shape.

**Could the caching be fixed instead?**
Yes, and it was considered: snapshot the overlay read per process so the merged
document is stable for the process lifetime.
It is rejected because it buys back only consequence 1.
Consequence 3 remains by construction, consequence 2 shrinks but does not close
(subagents and the supervisor sidecar start at arbitrary times, so their first read
can still land in a torn window), and the cost is a new caching mechanism in the
config loader plus four additional reader conversions, all to make a surface generic
that the ask does not need generic.

**Generality is reversible; exposure is not.**
Adding more applied keys later is one entry in a frozenset.
Removing `services:` overridability after an operator has used it in production is a
migration.
So the applied set starts at exactly what the ask requires.

Measured against the four blockers, and stated as round 5 judged it rather than as
round 4 hoped:

- **Blocker 1 is closed for `subscribe:`.** The 40 `Config.load` sites never see the
  overlay, so "restart to apply" holds for the applied set by construction.
  It is *not* closed for `managed_repos`, which the director reads live through
  `config show`; that is the two-clock consequence, stated below rather than claimed
  away.
- **Blocker 2 is reduced, not dissolved.** Round 4 claimed it shrank "to two boot-time
  reads where validation actually works". That was wrong: three of the five
  `subscribe:` read paths are not boot, and the supervisor process never runs boot
  validation at all. What actually closes it is `OverlayError` plus narrowing the
  swallow, specified below.
- **Blocker 3 is closed by the corrected table, not by the scoping alone.** The four
  other raw readers not seeing the overlay is correct by design, but round 4's table
  misclassified `registry.py:80`, which is a real content read of `monitors:`.
  Scoping made the divergence *inert*; correcting the table is what keeps a future
  widening from re-arming it.
- **Blocker 4 is closed outright.** There is no `Config._parse` interaction at all.

### The reader inventory, derived mechanically

The last three revisions each hand-listed the readers and each list came back
incomplete.
This one is derived by command, and the command is recorded so the next reader can
re-derive it rather than trust the table:

```bash
grep -rn "agent_yaml_path\|ROOT_MARKER" bobi/ --include=*.py | grep -v "^bobi/paths.py"
grep -rn "Config\.load(" bobi/ --include=*.py
```

Every hit of the first command is classified below.
The second command's hits are deliberately collapsed into one row, because none of
them see the overlay, with one exception that is called out separately: the
`Config.load` fallback inside the primary apply site
(`bobi/events/subscriptions.py:48`).
That fallback is where the previous revision's divergence lived, and it is the one
grep-2 hit the merge rule has to reason about.

| Site | What it does with the file | Sees the overlay? |
|---|---|---|
| `bobi/events/subscriptions.py:34` | reads `subscribe:`, primary path | **yes, applied** |
| `bobi/ingress.py:62` | reads `subscribe:` | **yes, applied** |
| `bobi/env.py:34` | reads `brain:` for child env | no, by design |
| `bobi/setup/actions.py:164` | reads `agent:` for a display name | no, by design |
| `bobi/monitors/script_cache_checks.py:649` | reads `script_cache:` | no, by design |
| `bobi/config.py:122` | `find_env_var_refs` scans `${VAR}` | no, but see below |
| `bobi/config.py:281` | `_project_config_path`, feeds `Config.load` (40 real call sites) | no, by design |
| `bobi/events/subscriptions.py:48` | `Config.load` fallback of the primary apply site | no, and it must not be reached; see the merge rule |
| `bobi/config.py:668`, `:684` | `_parse_build` resolves a sibling `Dockerfile` | not a content read |
| `bobi/monitors/registry.py:80` | `_read_records` (`:38-48`) reads `monitors:` | no, by design |
| `bobi/doctor.py:253` | maps a display name to a path | not a content read |
| `bobi/subagent.py:1591-1592` | `is_file()` root-marker check | not a content read |

Two corrections to the previous revision are folded in.
It claimed "the two raw readers"; there are six raw readers, and four of them
(`env.py`, `setup/actions.py`, `script_cache_checks.py`, `config.py:122`) went
unlisted.
It claimed "42 call sites" for `Config.load`; `grep -c` returns 42, but **two** of
those are docstring lines (`build_render.py:248` and `cli.py:188`), so 40 are real.

**This table is the invariant, and it is a test.**
Under this design the four unlisted raw readers not seeing the overlay is correct
rather than a divergence, because the overlay cannot set `brain:`, `agent:` or
`script_cache:` in the first place: a `brain:` key in the overlay is carried as data
and applied by nobody.
That is what makes `env.py:34`'s own warning inapplicable here.
Its docstring (`bobi/env.py:25-27`) says a divergence between it and `Config.load`
"would pass validate yet pin an empty gateway base URL into every child", and the
previous revision reproduced exactly that bug class inside its own fix.
A test asserts that `OVERLAY_APPLIED_KEYS` contains no key read by any of those four
sites, so widening the set later cannot silently recreate it.

### `find_env_var_refs`: union, not replace

`find_env_var_refs` (`bobi/config.py:114-130`) drives what `bobi validate` requires.
An overlay `subscribe:` entry carrying `${VAR}` would otherwise not be required, and a
pack `${VAR}` that the overlay replaces away would still be required.

**The union goes in `find_env_var_refs`, and nowhere lower.**
Round 5 killed the previous revision's placement.
It put the union in `_scan_env_refs` (`bobi/config.py:190-196`), which is impossible
and would have been harmful if forced:

- `_scan_env_refs(agent_yaml: Path)` receives a bare file path and has no
  `project_path`, so it cannot locate the overlay at all.
  `find_env_var_refs(project_path)` (`:114`) is the lowest function that can.
- Forcing it would corrupt a live cross-repo contract.
  `scan_required_vars` (`:211`) and `scan_declared_vars` (`:223`) also call
  `_scan_env_refs`, both documented for "a package file that isn't installed yet"
  (`:206-207`), and `scan_declared_vars` "doubles as the prune authority and the
  env-file filter" (`:220-221`).
  `moda-labs/moda-agents` calls both against *other teams'* source `agent.yaml` files
  at deploy time.
  Concatenating this runtime's overlay into that scan would corrupt another team's
  declared secret surface.

So: `find_env_var_refs` scans the pack, then scans the overlay's text with the same
`_ENV_VAR_RE`, and unions.
`_scan_env_refs`, `scan_required_vars` and `scan_declared_vars` are untouched.

`_build_only_names` (`:144-172`) computes `in_build - elsewhere`, and the overlay's
refs join `elsewhere`.

*Withdrawn premise.*
The previous revision justified this with "the overlay can never carry `build:` (it is
not in the applied set)".
That is false: not being applied does not stop an operator writing `build:` into a
free-form YAML file, and the scanner has no key awareness.
The conclusion survives on the safe-direction argument alone, which does not need it.

Union is the safe direction: a var referenced by either file is required.
Over-requiring a secret is an operator annoyance; under-requiring one is an outage.
This matches `_build_only_names`' own documented stance that "a classification bug
must over-require a secret, never quietly stop requiring one" (`:154-156`).

### Failure handling: atomic write, then fail loudly at boot

Two halves, and neither alone is enough.

**Write side.**
Every write of the overlay is atomic.
`bobi/fsutil.py` landed on main in #951 (`atomic_write_text`, `atomic_write_json`,
`file_lock`) and `CLAUDE.md` now requires durable state to go through it, so this is
adopting the house mechanism rather than inventing one.
The migration step writes the overlay with `fsutil.atomic_write_text`.
The framework has no other writer, because there is no `config set` CLI.

The director is not a framework writer and cannot be made one, so the companion prompt
carries the requirement in one line: write a sibling temp file and `mv` it over the
overlay, never edit in place.
This is stated as a known limit rather than a guarantee: a human with an editor can
still perform a non-atomic write, and no framework change prevents that.

**Read side.**
The previous revision claimed `subscribe:` is read "at exactly two sites, both during
manager startup".
That is wrong and is withdrawn.
There are five call paths and three of them are not boot:
`discover_subscriptions` from `service.py:534` (boot) and from
`supervisor/snapshot.py:99` (**every heartbeat, in the supervisor process**);
`explicit_subscriptions` from `ingress.py:109` inside `check_ingress_reachability`,
which is reached from `doctor.py:541` (`bobi doctor`, any time) and from
`service.py:185` in `build_startup_info`.

So a post-boot torn read is reachable, and the supervisor process never calls
`_load_config_or_raise` at all.
Boot validation alone does not cover it, and the previous revision's justification for
leaving the swallow in place was circular.

**The overlay therefore gets a typed error that no site may swallow into a default.**
`load_agent_yaml` distinguishes two cases: an *absent* overlay is normal and means
"pack wins", exactly today's behaviour; an overlay that is *present but unparseable*
raises `OverlayError`.

`bobi/events/subscriptions.py:44-45` must narrow its bare `except Exception: pass` so
`OverlayError` propagates.
Leaving it is not an option, because that handler falls through to the auto-detect
fallback, so a torn overlay would silently produce git-remote subscriptions, the same
failure the merge rule's presence gating exists to prevent.
With the narrowing, the supervisor's `except Exception: pass`
(`supervisor/snapshot.py:97-101`) yields the empty list its docstring promises
(`:92-93`) rather than a wrong non-empty one, so the heartbeat reports nothing instead
of reporting fiction.

At boot, `OverlayError` fails the boot loudly with the file and the parse error.

Boot validation lives in `_load_config_or_raise` (`bobi/service.py:248`), **not** in
`run_manager_from_config` (`:501`) as the previous revision specified.

**The reason is coverage, and the previous revision's reason was circular.**
It argued that `:501` was unreachable because `run_team_foreground` calls
`_load_config_or_raise` at `:484` first, so a malformed overlay "would have failed
earlier".
Round 5 leg C caught that: `:484` only fails earlier once validation is *already* at
`_load_config_or_raise`, which is the conclusion, not the premise.
The previous revision put validation at `:501`, so `:484` had nothing to fail on.

The non-circular argument is reachability, and it is stronger.
`_load_config_or_raise` has three callers, covering every way a manager starts:
`spawn_team` (`:348`, detached start), `start_team` (`:430`, waited start) and
`run_team_foreground` (`:484`, foreground).
The production container path lands on the third, twice over: the entrypoint execs
`bobi agent <name> supervise -- --foreground`
(`docker/docker-entrypoint.sh:630`), and the detached spawn itself re-enters through
`bobi agent <name> start --foreground` (`service.py:383-391`, spawned at `:398`).
`run_manager_from_config:501` is downstream of exactly one of the three.
One placement covers all of them.

Validation checks five things and no more: the document is a mapping; `for_agent`
matches the pack's `agent:` (`package/agent.yaml:130`); each applied key has the shape
its consumer expects; an applied key present with a null value is rejected rather than
read as empty; and no top-level key is a framework-parsed key outside
`OVERLAY_APPLIED_KEYS`.

**The `for_agent` guard exists for pack swap, and it is required, not optional.**
`seed_workspace` (`bobi/install.py:118-136`) only adds; nothing removes.
Installing a different team into the same run root would leave a stale overlay
replacing keys in a document that no longer means the same thing.
One line in the overlay and one check at boot turns that from a silent boot disaster
into a named failure.

Round 4 made it optional ("if present").
An optional guard does not guard: the disaster it exists for happens precisely on a
hand-written overlay, so the field would be absent exactly when it is needed.
It is therefore **required whenever the overlay defines any applied key**.
A seed template that defines no applied keys does not need it, which is why the
template can ship without one.

### Read-back: `bobi agent <name> config show`

Generic, read-only, per ruling 5.
It prints the effective document with each top-level key marked `pack`, `overlay`, or
`overlay-data`.
`--source` prints the tier column alone; `--json` prints a machine-readable form.

**The output is a contract, because a frozen prompt parses it.**
The companion prompt tells the director to read `managed_repos:` from this command, so
the format is specified here rather than left to the implementation: keys in the
merged document's own order, `--json` emitting
`{"key": {"value": ..., "source": "pack"|"overlay"|"overlay-data"}}`.
The prompt uses `--json`.
One vocabulary for the data tier, `overlay-data`, in both the human and the JSON view.

**It prints the RAW, uninterpolated document.**
`services:` carries `credentials:` (`package/agent.yaml:10-11`, `:16-18`, `:22-23`), so
an interpolated view would print `GH_TOKEN`, `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`
and `LINEAR_API_KEY` in plaintext to a terminal and into an agent transcript.
Uninterpolated, those stay `${GH_TOKEN}` and nothing is disclosed, which is also the
right view for "what does the config say" and matches the merged document the loader
returns.

**An unparseable overlay makes it exit non-zero with the parse error.**
It must never degrade to a pack-only view.
`config show` is not on the boot path, so it never runs boot validation, and the
director reads *write authority* from its output: silently falling back to the pack's
`managed_repos` would hand the director a wrong authority list with no signal.

**The command has to be attached, not just declared.**
`bobi/cli.py` builds the `agent` group from two hardcoded lists: single commands at
`:3557-3563` and groups at `:3565-3567`, followed by `:3573-3578`, which pops the old
top-level alias.
A new `@main.group() def config()` following the `monitors list` precedent is *not*
reachable as `bobi agent <name> config show` until `"config"` is added to the group
list at `:3565` and to the pop list at `:3573-3577`, which is exactly how `monitors`
appears in both.
`config` is otherwise free: no command or group of that name exists in `bobi/cli.py`.
This is named because the previous revision cited the decorator (`:2417-2419`) and not
the attach point, and a frozen prompt depends on this command resolving.

The previous revision justified this command by claiming `doctor.py:253` "keeps
reporting pack-only truth".
That is withdrawn: `:253` is inside `_check_runtime_layout` (`:244-258`), a layout
existence check that reads no file content.
The real justification is simpler and stands on its own: without it, every human and
every prompt hand-merges two files.

### Live effect: a restart is required, and now that is true

**Adding a repo takes effect after a manager restart.
This is not hot reload.**

Under this design the sentence is true by construction, not by convention: the applied
set is `{subscribe}`, and `subscribe:` is read at `bobi/service.py:534` once, then
handed to the spawn (`:664`).
The reconnect path re-asserts an in-memory list rather than re-reading config
(`bobi/events/client.py` `_needs_resubscribe`).

**But one file edit now lands on two clocks, and that is a real consequence.**
The previous revision claimed the supervisor heartbeat was "the only place the two can
disagree".
That is wrong and is withdrawn.

`subscribe:` is applied at restart.
`managed_repos` is read by the director through `config show --json` on the next
invocation, per the companion prompt.
So an edit changes *authority* immediately and *event routing* at the restart.

The offboard direction is the dangerous one.
An operator removes repo X from both lists at T0.
Authority drops at once; routing persists until the restart.
In that window the manager is still subscribed to X, and `auto_dispatch` still arms
`issue-lifecycle` (`package/agent.yaml:101-104`) and `pr-closed` (`:105-110`) with
`allow_self_authored: true`, so workers can launch against a repo the director now
believes it does not manage, under a branch-delete guard whose scope is defined by the
list that already changed.
The onboard direction is benign: authority arrives before routing does.

The window is bounded by the restart and the mitigation is operational, not
mechanical: **offboard by editing the overlay and restarting immediately, then confirm
with `config show`.**
The docs state this as the offboarding procedure rather than leaving the ordering to
the operator.

*The smaller exception, for completeness.*
The supervisor sidecar's `_expectations` (`bobi/supervisor/snapshot.py:88-99`) calls
`discover_subscriptions` on every heartbeat, inside `except Exception: pass`.
It changes no behaviour; it reports what the manager is expected to be subscribed to,
and the read model diffs that against traffic to derive silence.
Between an edit and the restart it reports the intended set while the manager holds
the old one, so a not-yet-subscribed topic can briefly show as silent.

**The restart is cheap only when the newly added topic already has its #488 grant.**
The happy path is real: the manager session is preserved (cleared only on `--fresh`),
and the saved-deployment path PUTs the newly authorized list against the existing
`deployment_id` (`bobi/subagent.py:1422-1431` against a local event server,
`:1473-1482` against a hosted one), so the cursor survives and events arriving during
the gap replay rather than drop.

The previous revision stated that unconditionally, and round 5 leg C falsified it.
Onboarding is precisely the case where the condition fails, because the repo is new:

1. `authorize_resources` runs with `filter_unauthorized=False` (`subagent.py:1417`,
   `:1464`), so a topic whose grant is denied is **kept** in the PUT body rather than
   dropped (`bobi/events/server.py:719-728`).
2. `github:` is a global topic (`event-server/core/src/core.ts:350`), so the server
   demands a matching resource grant and **rejects the whole update** with a 400 on
   any ungranted topic - deliberately, with no partial write (`core.ts:1438-1444`,
   `unauthorizedGlobalTopics` at `:1836-1854`).
3. `resp.raise_for_status()` raises (`subagent.py:1431`, `:1482`) and both handlers
   fall through to `_register_with_retry` (`:1436`, `:1486`).
4. `_register_with_retry` deletes the event cursor itself (`subagent.py:1359-1361`:
   "A fresh deployment starts a fresh seq space - a leftover cursor would skip or
   mis-replay events on first connect") and mints a new `deployment_id`/`api_key`.

So adding `github:moda-labs/newrepo` to the overlay and restarting **before** the
GitHub App is installed on that repo does the opposite of what the paragraph above
promises: the cursor is destroyed and the deployment identity is re-minted, leaving
one WARNING line in `state/manager.log`.
Both branches land there.
The local-server branch takes an extra explicit unlink at `:1435` on the way; the
hosted branch, which is what the fleet runs, gets there through `:1486` alone.
Round 5 leg C attributed the loss to `:1435` and therefore to the hosted path as well;
the citation is off by one branch, but the conclusion is right and the real mechanism
is worse, because it does not depend on which branch runs.

**Therefore onboarding has a prerequisite, and the docs state it before the edit, not
after:** install the GitHub App on the repo (or provision the equivalent
`linear:`/`slack:` credential coverage) **first**, then edit the overlay, then restart.

Named as an honest gap: there is no pre-flight command today that reports whether a
topic holds its grant, and the 400's body already names the offending topics
(`{"error": "unauthorized_topics", "topics": [...]}`) while
`raise_for_status()` discards them into a generic message.
Making onboarding a one-line edit turns this from a rare deploy-day event into a
routine one, so surfacing that body is folded into the implementation plan below.

Two operator consequences the docs must state, not bury:

- A restart can strand in-flight runs.
  The instruction is "restart, then check the runs table and resume stranded runs"
  (`docs/RUN_RESUME.md`).
- The first restart after onboarding a busy repo is when its backlog can fan into
  auto-dispatch, because `auto_dispatch` (`package/agent.yaml:78`) arms
  `issue-lifecycle` (`:101-104`) and `pr-closed` (`:105-110`) with
  `allow_self_authored: true` for every subscribed repo.
  Expect worker launches on a newly onboarded repo.

The identity-preserving live path, named so a follow-on need not rediscover it:
`PUT /deployments/<id>/subscriptions {"replace": [...]}` (`bobi/subagent.py:1473-1474`,
route `event-server/worker/src/index.ts:650`).
`register()` is the wrong primitive: it supersedes the deployment and mints a new
`deployment_id`/`api_key`.
A watcher belongs in the manager process next to `MonitorScheduler`, must reassign
`active_subscriptions` (`:1483`) because `_resubscribe_on_deaf` replays it
(`:1509-1511`), and must not re-invoke the saved-deployment sync path, whose `except`
unlinks the event cursor (`:1435`).
Correction to the previous revision: `:1486` does **not** unlink the cursor, it calls
`_register_with_retry`.

### Authority, in one line as ruled

Write authority moves into the overlay with everything else.
Branch-delete safety is policy and prompt, not machinery: the guard is the role
prompt's decline-on-human-branch default, and nothing here enforces or weakens it.

**The consequence, named rather than left implicit.**
Ruling 3 is settled and this does not reopen it, but the spec owes the shape change
out loud: `<run>/workspace/` is agent-writable (`drwxr-xr-x bobi bobi`, and agents run
as `bobi`), while `package/agent.yaml` is a read-only frozen image.
Before, adding a repo to `managed_repos` took a reviewed PR to `moda-labs/moda-agents`
plus a reinstall.
After, one unreviewed agent turn writing one line grants it.
The authority list becomes self-modifiable by the thing it governs, and the guard is
the same policy/prompt layer ruling 4 places the branch-delete default in.
Accepted under ruling 3, with no mechanical mitigation, deliberately.

### The companion PR is a blocking co-deliverable

Verified: `managed_repos` has zero consumers in `bobi/` **and zero in the installed
pack beyond its own definition** (`run/package/agent.yaml:125`).
No role prompt, tool doc, workflow or monitor reads it; the director's belief about
write authority comes from long-term memory.

So moving the list changes nothing on its own.
Until the prompt reads the overlay, there are two lists and the stale one governs.
The PR in `moda-labs/moda-agents` is **blocking**, and it carries two changes, not one:

1. **The prompt instruction**, specified verbatim rather than left to interpretation:

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
   > `BOBI_ROOT` is pinned into every agent's environment (`bobi/env.py:162`) and the
   > installed agent name is its parent directory.
   > That is NOT the same string as the pack's `agent:` field, so do not substitute
   > one for the other.
   >
   > Before adding a repo to `subscribe:`, confirm the GitHub App is installed on it.
   > Restarting with an ungranted topic re-mints the deployment and loses the replay
   > window.
   >
   > When you edit the overlay, write a sibling temp file and `mv` it over the target.
   > Never edit it in place.

   Three things in that text are load-bearing and were missing before.
   The applied-key restriction is stated where the director reads it, rather than only
   in this document.
   The name is *resolved* rather than left as `<name>`: this deployment's installed
   name is `eng-team` while its pack's `agent:` is `moda-eng-team`, so a prompt that
   guessed would have picked the wrong one.
   And the JSON path is `.managed_repos.value`, not `.managed_repos`, because of the
   `{"value": ..., "source": ...}` shape specified above.

2. **The seed template**, `agents/moda-eng-team/workspace/overlay.yaml`.
   The live pack has no `workspace/` directory at all, so this is a new directory in
   the pack, not a file added to an existing one.
   The previous revision counted this as part of step 5 and undercounted the pack-side
   work.

## Migration

Write `<run>/workspace/overlay.yaml` once, containing the complete current
`subscribe:` list and the current `managed_repos:` list, copied from
`run/package/agent.yaml` in the pack's own order (lightweave, moda-agents,
bobi-agent, moda-skills), plus `for_agent: moda-eng-team`.
Behaviour on day one is unchanged.

To be precise: consumption is order-insensitive (`bobi/service.py:543-545`, `:549-551` are
membership tests), so this is a semantic no-op rather than a literal byte match.

`moda-labs/familystories-ai` appears in neither list.
It was offboarded 2026-08-04 22:24 UTC (`c0ec10a` in moda-agents) because baohua is
its sole engineering team.
Verified genuinely off: parsing the director's event log by
`payload.repository.full_name` over a 33-hour window shows 628 familystories events,
newest `2026-08-04T21:29:36`, and **none** after the 22:37:54 pack reinstall.
No ongoing leak.
(A substring grep for `familystories` inflates this badly, because the string appears
inside `moda-labs/bobi-agent` issue and PR payloads.)

The commented seed template ships in the pack's new `workspace/`.
It is safe because it defines no keys, and it is how an operator discovers that the
file and the merge rule exist.
Its comments carry the applied-key list, so the one thing a template can do that
documentation cannot is done there: tell the operator, at the point of editing, that
`services:` will not take effect.
`seed_workspace()` copies it only if absent and is otherwise untouched.

The pack's `subscribe:` and `managed_repos:` stay in place as the seed layer for a
fresh deployment that has no overlay yet.

**The fleet trade, named.**
For one deployment this is a clear win: 22 minutes and a cross-repo PR become one file
edit and a restart.
For N deployments it replaces a PR-reviewed, atomic, version-controlled change with N
hand edits on N durable volumes (`Dockerfile:283` `VOLUME ["/data"]`), with no drift
detection and no audit trail.
That is the deliberate cost of the ask, it is accepted, and the mitigation if the
fleet grows is to converge the overlays from a source of record rather than to
re-pack.

## Verification plan

- Unit: an overlay key in the applied set replaces the pack's key; absent keys fall
  through; an absent overlay is exactly today's behaviour.
- Unit: an overlay key outside the applied set that **no framework code parses**
  (`managed_repos`) is carried as data, changes no runtime behaviour, and is named in
  boot's applied-vs-data log line.
- Unit: an overlay key outside the applied set that the framework **does** parse
  fails boot with the key named.
  Parametrized over `roles`, `services`, `brain`, `monitors`, `build`, `requires`,
  `host`, `auto_dispatch` and `agent`, so the case the director actually hits
  (`roles:` for a cheaper model, against a pack that sets `claude-opus-5` at
  `agent.yaml:113-116`) is covered rather than assumed.
- Unit, the predicate behind that rejection: every key in `Config._parse` and every
  key the four raw readers read is either in `OVERLAY_APPLIED_KEYS` or rejected.
  This is what stops the two sets drifting apart as `Config` gains fields.
- Unit: `${VAR}` in an overlay `subscribe:` entry interpolates identically to the pack.
- Unit: a malformed overlay fails manager boot loudly with the file and the reason, at
  `_load_config_or_raise` (`bobi/service.py:248`), and is not swallowed by
  `subscriptions.py:44`'s
  `except Exception: pass`.
- Unit: `for_agent` mismatching the pack's `agent:` fails boot with a named error.
- Unit: `find_required_env_vars` unions pack and overlay refs.
  An overlay `${VAR}` becomes required; a pack `${VAR}` the overlay replaced away stays
  required.
- Unit, the invariant behind the reader table: `OVERLAY_APPLIED_KEYS` contains no key
  read by `env.py:34`, `setup/actions.py:164`, `script_cache_checks.py:649`,
  `monitors/registry.py:80` or `Config.load`.
  `registry.py` is in that list because `_read_records` (`:38-48`) is a raw
  `yaml.safe_load(...).get("monitors")`, so adding `monitors` to the applied set would
  diverge three readers at once: `MonitorRegistry.load`, `Config._parse`'s
  uninterpolated `monitors_raw` (`config.py:564`), and the overlay.
  This is the test that stops a future widening from recreating the divergence
  `env.py:25-27` warns about.
- Unit: `config show --json` emits the specified shape, and `managed_repos` resolves
  from the overlay.
  The frozen prompt parses this output, so the format is under test.
- Unit, the attach point: `bobi agent <name> config show` resolves through the CLI's
  runner, not merely as a declared group.
  A decorator alone leaves it unreachable until `"config"` joins the lists at
  `cli.py:3565` and `:3573-3577`, and the frozen prompt invokes exactly this form.
- Unit: the prompt's own invocation works end to end - resolving the agent name from
  `BOBI_ROOT` yields the installed name (`paths.agent_name_for_root`,
  `paths.py:95-97`), and `.managed_repos.value` is the path that returns the repo
  list.
  The prompt is frozen text against a machine-readable contract; nothing else tests
  that the two agree.
- Regression, the resurrection trap: pack `subscribe:` re-declaring an offboarded repo
  does not resubscribe it while the overlay defines `subscribe:`.
- Regression, the framework-purity constraint: no `managed_repos` and no `tracker:`
  **as an agent.yaml key** appear under `bobi/`.
  Stated as a precise predicate, not prose, because the loose version is untestable:
  `tracker` already appears at `setup/services.py:307` and on nine lines of
  `events/drain.py` (`_AckWatermark`), and `repo` appears in 20+ modules.
  The predicate is: the literal strings `managed_repos` and `OVERLAY_APPLIED_KEYS`'
  forbidden members do not appear in `bobi/`.
- Regression: `seed_workspace()` unchanged; the seed template defines no keys.
- Integration: a full manager start against the local event server registers exactly
  the overlay's `subscribe:` set, and a repo removed from the overlay is genuinely
  unsubscribed after restart rather than merely absent from the file.
- Integration, the contract blocker 1 falsified.
  The obvious form of this test is **vacuous** and round 5 caught it: asserting that
  live *subscriptions* do not change mid-process passes under both designs, because
  `subscribe:` is captured into a local at `service.py:534` and handed to the spawn at
  `:664` either way.
  The assertion has to land on a `Config.load` consumer, which is what the chokepoint
  design would have made hot: edit the overlay while a manager runs and assert
  `launch_admission` (`subagent.py:272`), `max_launch_depth` (`launch_lineage.py:244`)
  and `entry_role` (`monitors/scheduler.py:419`) still resolve to their pack values.
  That fails against the chokepoint design and passes against this one.
- Integration, presence gating: a manager booted with an overlay `subscribe: []`
  subscribes to nothing and does **not** fall through to git-remote auto-detection.
  This is the test that reproduces the round-5 blocker; without presence gating it
  returns `github:<whatever run/ happens to contain>`.
- Unit: an overlay `subscribe:` with a null value (the mis-indented-list typo) fails
  boot, and is not read as an intentional empty list.
- Unit: the two apply sites agree on an empty, an absent, and a populated `subscribe:`.
  They disagree today (`ingress.py:75-79` returns `[]`, `subscriptions.py:42` falls
  through), so this is a regression test for a difference the overlay would otherwise
  make visible.
- Unit: `OverlayError` from a torn overlay propagates through
  `subscriptions.py:44-45` rather than being swallowed into the auto-detect fallback,
  and the supervisor's handler yields the empty list its docstring promises.
- Integration: a torn overlay (truncated mid-write) present at boot fails the boot with
  a named error rather than starting on pack subscriptions.
- Integration: the #488 resource-grant path (`bobi/events/server.py:612`) authorizes
  overlay-sourced topics on the restart path.
  The obvious form of this test is **wrong**, and round 5 leg C caught it: asserting
  that "an ungranted topic is reported rather than silently dropped" describes the
  `filter_unauthorized=True` branch, which the restart path never takes.
  The restart path passes `filter_unauthorized=False` (`subagent.py:1417`, `:1464`),
  keeps the ungranted topic, and the server 400s the whole PUT.
  So the assertion is on the consequence: a manager restarted with an overlay
  `subscribe:` entry that has no grant loses its event cursor and re-mints its
  deployment, and the operator-visible log names the ungranted topics rather than
  reporting a bare 400.

## Implementation plan

1. `bobi/agent_config.py`: `load_agent_yaml(project_path) -> dict` returning the merged
   **uninterpolated** document, plus `OVERLAY_APPLIED_KEYS` and the validation entry
   point.
2. Apply it at the two `subscribe:` sites: `bobi/events/subscriptions.py:34` and
   `bobi/ingress.py:62`, gating on **presence, not truthiness**, so a defined-but-empty
   `subscribe:` means "nothing" rather than "auto-detect", and the two sites agree.
   Narrow `subscriptions.py:44-45` so `OverlayError` propagates.
   No other reader changes.
3. Boot validation in `_load_config_or_raise` (`bobi/service.py:248`), which covers all
   three start paths (`:348`, `:430`, `:484`).
   Five checks, including the rejection of a framework-parsed key outside
   `OVERLAY_APPLIED_KEYS`, plus the applied-vs-data log line for the keys that pass.
4. `find_env_var_refs` (`config.py:114`) unions the overlay's `${VAR}` refs with the
   pack's, and `_build_only_names` folds overlay refs into `elsewhere`.
   `_scan_env_refs`, `scan_required_vars` and `scan_declared_vars` are untouched: they
   serve not-yet-installed packages and a live cross-repo contract.
5. `bobi agent <name> config show`, with `--source` and `--json`, following
   `monitors list` (`bobi/cli.py:2417-2419`), **and attached**: `"config"` joins the
   group list at `cli.py:3565` and the top-level pop list at `:3573-3577`.
   The decorator alone leaves the command unreachable.
6. Surface the ungranted topics on a failed subscription PUT.
   The server returns them in the 400 body (`core.ts:1443`) and both handlers
   (`subagent.py:1433-1436`, `:1484-1486`) currently log only
   `raise_for_status()`'s generic message before discarding the cursor.
   In scope because this change is what makes the ungranted-topic case routine: the
   onboarding it enables is a one-line edit away from producing it.
7. Migration overlay written into the live runtime with `fsutil.atomic_write_text`.
8. Docs: `docs/BUILDING_AGENT_TEAMS.md` gains a runtime-overlay section covering the
   merge rule and its three outcomes, the restart consequences above, the **grant
   prerequisite before an onboarding edit**, the two-clock offboarding procedure, the
   supervisor-heartbeat exception, and one more consequence of the env-ref union: an
   overlay referencing an unset `${VAR}` now fails the whole manager boot at
   `_validate_or_raise` (`service.py:488`), not just the subscription.
   Nothing to rewrite: `managed_repos` does not appear in `docs/` at `origin/main`.
9. **Blocking co-deliverable:** the `moda-labs/moda-agents` PR carrying both the prompt
   text and the `workspace/overlay.yaml` seed template.

Complexity is now small.
The previous revision's size came entirely from routing 40 `Config.load` sites and six
raw readers through one loader; scoping the applied set to `subscribe:` removes that
work rather than deferring it.

## Review record

**Spec review gate.**
The house binding is `/gstack-plan-eng-review`, `/gstack-plan-design-review` and
`/gstack-plan-ceo-review`.
Those skills are interactive and cannot run in a headless worker, so the three lenses
ran as adversarial passes instead: architecture/edge-cases/tests, design/operator
experience, and scope.
A labelled substitution, not a claim to have run the skills.
Not plan-born (no plan artifact referenced, no matching bracket prefix), so all three
lenses apply.

**Round 4 is what produced this revision.**
It returned four blockers, all rooted in the `Config`-as-chokepoint decision, and the
decision is reversed above rather than patched.
Every one of its citations was re-verified first-hand against the rebased tree before
being acted on, and `script_cache_checks.py`'s read had already moved from 651 to 649
under #951.
The fabricated `paths.py` walk justification (M1), the `config.yaml` name collision
(M2), the unreachable validation placement (M3), the `find_env_var_refs` gap (M5), the
pack-swap guard (M6), the fleet trade (M7), the `doctor.py:253` mischaracterization,
the `config show` output contract, the uncounted second pack-side deliverable, and the
`subagent.py`/`agent.yaml` citation drift are all folded in above.

**Round 5 reviewed the reversal itself and returned four more blockers.**
It executed the critical path rather than reasoning about it, and its verdict on
round 4's four was: B4 genuinely closed, B1 closed for `subscribe:` but reopened on a
different axis, B2 relocated, B3 not closed.
All four are folded above:

| Round-5 finding | Disposition |
|---|---|
| Empty `subscribe:` falls through to git-remote auto-detection (`subscriptions.py:42` gates on truthiness) | **Accepted.** Presence gating is now part of the merge rule and step 2. |
| Torn overlay swallowed into that same fallback; the supervisor re-reads post-boot | **Accepted.** `OverlayError`, and `subscriptions.py:44-45` must narrow. The circular "already parsed at boot" justification is withdrawn. |
| Two clocks: `subscribe:` at restart, `managed_repos` immediately | **Accepted.** "The only place the two can disagree" is withdrawn; the offboard window and its procedure are stated. |
| The env-ref union cannot live in `_scan_env_refs`, and would corrupt a cross-repo contract | **Accepted.** Moved to `find_env_var_refs`; the false "overlay can never carry `build:`" premise is withdrawn. |
| The "blocker 1 falsified" test is vacuous under both designs | **Accepted.** Retargeted at `Config.load` consumers. |
| `config show` had no failure mode and no redaction statement | **Accepted.** Raw/uninterpolated, non-zero exit on an unparseable overlay. |
| `registry.py:80` misclassified as "not a content read" | **Accepted.** It reads `monitors:` via `_read_records` (`:38-48`); table corrected and it joins the invariant test. |
| `workspace/` is agent-writable, so the authority list is self-modifiable | **Accepted as a stated consequence** under ruling 3, no mechanical mitigation. |
| 41 real `Config.load` sites | **Corrected to 40.** Two docstring lines match the grep, not one. |
| `for_agent` optional; data-tier vocabulary split; `doctor.py:244`; `drain.py` count | **Accepted**, all folded. |

**Round 5 ran three legs, and the third was orphaned.**
Legs A and B returned in time and produced the fold above.
Leg C (`adhoc-0b6ba091`) was launched later against the same revision and was still
running when its launcher exited, so its findings reached nobody until they were
recovered from its transcript.
All three legs are committed verbatim under `plans/reviews/` rather than summarized,
because a review nobody received is the failure this record exists to prevent.

Leg C confirmed the great majority of the spec's citations independently, agreed with
A and B on `registry.py:80`, the 40 `Config.load` sites and the `drain.py` count, and
returned three blockers of its own:

| Leg-C finding | Disposition |
|---|---|
| B1: the reversal *moves* the torn-read blocker to `snapshot.py:99` on a 30s timer rather than closing it | **Already closed** by the A/B fold, before leg C was read: `OverlayError` plus the narrowed swallow, and the five call paths named. Leg C reached the same conclusion independently. |
| B2: the frozen prompt states a universal merge rule the design does not implement, so `roles:`/`services:`/`brain:`/`monitors:` are silently inert | **Accepted.** Boot now rejects a framework-parsed key outside the applied set, the merge rule states all three outcomes, and the frozen prompt carries the restriction. |
| B3: "events replay rather than drop" is false when the PUT 400s on a missing #488 grant | **Accepted, with the mechanism corrected.** Leg C traced the cursor loss to `subagent.py:1435`, which is the local-server branch; the hosted branch the fleet runs reaches `:1486`. Verified first-hand that `_register_with_retry` unlinks the cursor itself (`:1359-1361`), so both branches lose it and the finding is stronger than stated. Prerequisite and failure mode are now in the restart section. |
| M3: `config show` is unreachable until `"config"` joins the CLI attach lists, and the prompt has no `<name>` resolution and no JSON path | **Accepted.** Attach point named (`cli.py:3565`, `:3573-3577`); the prompt resolves the name from `BOBI_ROOT` and reads `.managed_repos.value`. This deployment's installed name is `eng-team` while its pack's `agent:` is `moda-eng-team`, so the unresolved `<name>` was a live trap. |
| M4: the "unreachable placement" argument for `_load_config_or_raise` is circular | **Accepted.** Replaced with the three-caller coverage argument (`:348`, `:430`, `:484`) and the container path. |
| `_scan_env_refs` is `190-196`; `snapshot.py:48` is `_team_package_version` and yields a null `versions.team_package`, not an empty heartbeat | **Accepted**, both corrected. |
| Two stale "41 `Config.load` sites" survived the A/B correction to 40 | **Accepted**, both fixed. |
| The seed template's adjacent lines contradict each other | **Partly refuted.** The next line already restricted the rule. The template is rewritten anyway, because the frozen prompt - which had no restriction at all - was the real gap. |
| M1 (`subscribe:` read at "exactly two sites"), M2 (`registry.py:80`), MINOR 2 (`setup/actions.py` "169 to 171"), MINOR 3 (the `build:` premise) | **Already addressed** in the A/B fold; not re-applied. |

**Earlier rounds.**
Rounds 1 and 2 ran against the pre-ruling `repos.yaml` design and returned five
blockers before Zach's premise ruling replaced it entirely.
Round 3 produced the generic-merge revision round 4 reversed.

**Cross-model pass: still owed.**
`codex exec` returns `401 Unauthorized: Missing bearer or basic authentication` in this
container, `aichat` has no provider config, and there is no fallback LLM key.
Verified first-hand, not assumed.
Same-model passes are a fallback, not a substitute, and this is recorded as an
outstanding debt rather than waived.
