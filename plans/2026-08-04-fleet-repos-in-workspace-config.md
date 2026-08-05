# Runtime workspace overlay for agent config

Issue: [#952](https://github.com/moda-labs/bobi-agent/issues/952)
Status: spec, awaiting approval (Gate 1)
Created: 2026-08-04. Rewritten 2026-08-05 under Zach's premise constraints.

## Problem

Onboarding or offboarding a repo means editing agent pack source in a different
repository and redeploying.

The bindings live in `agents/moda-eng-team/agent.yaml` in `moda-labs/moda-agents`,
installed to `run/package/agent.yaml` as a read-only frozen image, in two lists:
`subscribe:` (routes events) and `managed_repos:` (per-repo `tracker:`, and in
practice write authority).

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

This is a rewrite. The previous version proposed a `workspace/repos.yaml` registry plus
a `bobi agent <name> repos add|remove|list` CLI. Zach rejected the premise:

> in this design, it looks like pack-specific concepts like repo management have
> leaked into the main bobi agent framework. bobi-agent should NOT have a CLI
> command like `bobi agent <name> repos …`, and if we do have `seed_workspace()`
> it should do it in a general purpose way, not specifically for `managed_repos`

> TBH I'm not that worried about accidentally deleting human branches, they are
> always recoverable. I am more worried about anything that links repos to
> requiring PRs. I think not deleting human branches is more of a policy/prompt thing

Binding rulings:

1. **No repo-specific concepts under `bobi/`.** No `repos` CLI, no `managed_repos`
   awareness in framework code.
2. **`seed_workspace()` stays general purpose.** Verified it already is
   (`bobi/install.py:118`, copy-only-if-absent), so the correct action is to change nothing.
3. **Write authority moves to workspace with everything else.** Keeping it pack-side
   would mean managing a repo still needs a PR to the pack repo. This reverses the
   previous spec's recommendation.
4. **Branch-delete safety is policy/prompt.** No machinery: no manifest hashing, no
   drift detection, no install-time reversion.

### The tension this resolves

Zach wants zero repo-specific concepts in the framework, but subscription routing *is*
framework machinery. The resolution is to make the framework's contribution **generic**:
one runtime config layer that knows nothing about repos, and a pack that supplies all
meaning.

This also rules out the tempting shortcut of allowlisting `subscribe:` and
`managed_repos:` by name in the loader. `managed_repos` is not a framework key, so
naming it in `bobi/` would be exactly the leak ruling 1 forbids. The merge must be
generic over all keys.

## Solution

Add one generic primitive: **a workspace overlay merged over the installed `agent.yaml`.**

`<run>/workspace/config.yaml`

```yaml
# Runtime overlay over package/agent.yaml.
# A top-level key defined here REPLACES that key entirely. Restart to apply.
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

The framework merges. It never learns what a repo is. Onboarding becomes: add two lines
to one workspace file, restart. No pack edit, no PR to `moda-labs/moda-agents`, no
rebuild, no reinstall.

**Not `workspace/agent.yaml`.** `paths.ROOT_MARKER = "agent.yaml"` (`bobi/paths.py:23`)
and runtime-root detection walks for that filename (`:112`, `:132`, `:140`), so a file
by that name in the runtime root's subtree is a collision hazard.

### Merge rule: replace, not deep-merge

**A top-level key present in the overlay replaces the pack's value for that key.**
One rule, uniformly applied, no per-key logic:

- It keeps the framework free of domain semantics. Deep-merge would need to know which
  keys are sets, which are ordered, and what identity means for a list element - exactly
  where repo knowledge would leak in.
- It closes the resurrection trap by construction. A pack upgrade re-declaring an
  offboarded repo in `subscribe:` cannot revive it, because the pack's `subscribe:` is
  never consulted once the overlay defines that key. No tombstones, no second mechanism.
- It is predictable to a human editing YAML.

**Trade-off, accepted deliberately.** A pack upgrade adding a genuinely new entry to an
overridden key does not take effect until an operator copies it across. Silent pack-side
additions to a list the operator now owns are the same class of surprise as silent
resurrection. The migration therefore copies the *complete* current `subscribe:` list,
including `slack:` and `linear:`, so the overlay is whole rather than partial.

**Interpolation:** overlay values interpolate `${VAR}` exactly as the pack does
(`bobi/config.py` `_interpolate_env`). The previous draft proposed keeping the overlay
literal; that was wrong. `discover_subscriptions` already interpolates whatever
`subscribe:` list it is handed (`bobi/events/subscriptions.py:40`), so a literal overlay
would need a *second* code path, and env-var scanning would miss overlay refs. One path,
one rule.

## Scope

In scope: the shared loader and merge; converting the framework's content readers;
a read-only effective-config view; a commented seed template; migration; docs; and the
companion prompt PR in `moda-labs/moda-agents`.

Out of scope, each with a reason:

- **Any `repos` CLI, or any `config set` CLI.** Ruled out. The overlay is a YAML file;
  humans and the director edit it. Conversational onboarding is the operator surface.
- **Changes to `seed_workspace()`.** Already general purpose.
- **Live reload without a restart.** Deferred; correct insertion point named below.
- **Machinery for branch-delete safety.** Policy/prompt, per ruling 4.
- **Per-key validation of which keys may be overridden.** That is where repo semantics
  would leak in. Handled as documentation, per ruling 4.

## Technical approach

### `Config` is the chokepoint

The earlier draft proposed a new `agent_config.py` loader and listed seven call sites to
convert. That inventory was **wrong**: it omitted `Config.load` (`bobi/config.py:549`,
via `_project_config_path` at `:279`), which has **42 call sites** in `bobi/` and parses
`services`, `auto_dispatch`, `brain`, `monitors`, `requires`, `host`, `build`,
`channels`, `max_concurrent_agents`, `spend_cap`.

Omitting it would produce precisely the divergence the spec claims to prevent, and the
sharpest form is inside one function: `discover_subscriptions` reads the raw document on
its primary path (`bobi/events/subscriptions.py:34`) and falls through to
`Config.load(project_path)` at `:45` for adapter auto-detection. Merged primary,
unmerged fallback, same function.

So:

```python
# bobi/agent_config.py
def load_agent_yaml(project_path: Path) -> dict:
    """Installed agent.yaml with the runtime workspace overlay applied.

    A top-level key in workspace/config.yaml replaces the pack's value.
    """
```

`Config.load` uses it, which carries the merge to all 42 sites for free. The two raw
readers - `bobi/events/subscriptions.py:34` and `bobi/ingress.py:62` - use the same
helper. That is one code path for "what does agent.yaml say".

Sites that are **not** content reads and stay as they are:
`bobi/subagent.py:1591-1592` (`is_file()` root-marker check) and `bobi/doctor.py:253`
(maps a display name to a path for manifest drift).

`bobi/monitors/registry.py:80` passes a **Path** into `_read_records()` alongside a
sibling path source, so it needs a records-from-dict path rather than a mechanical
swap. Called out because "convert the readers" reads as mechanical and this one is not.

`bobi/build_render.py:257` `load_composed_team_config` is **build-time** pack
composition and is deliberately untouched: this is a runtime layer.

### Failure handling: validate once, at boot

A loader that raises is not enough. Five of the converted sites swallow exceptions
today - `bobi/events/subscriptions.py:43` is a bare `except Exception: pass` on the
feature-critical path - so a raise would be silently absorbed and the fleet would
quietly revert to pack subscriptions, which is the exact failure this change exists to
prevent.

Therefore: **parse and validate the overlay once at manager startup**, in
`run_manager_from_config` (`bobi/service.py:500`) before any subscription work, and fail
the boot loudly with the file, line and reason. Per-site handlers keep their current
behaviour. An *absent* overlay is normal and means "pack wins" - today's behaviour exactly.

Validation at that point also rejects a non-mapping document and reports unknown-shaped
entries, which is where an operator's typo actually shows up.

### Read-back: `bobi agent <name> config show`

Generic, read-only, prints the effective merged document with each top-level key marked
`pack` or `overlay`. This is not a repo concept and does not violate ruling 1.

It exists because without it every human and every prompt hand-merges two files, and
`doctor.py:253` keeps reporting pack-only truth while the runtime uses something else.
It is the debuggability counterpart to failing loudly.

### Live effect: a restart is required, and this says so

**Adding a repo takes effect after a manager restart. This is not hot reload.**

`discover_subscriptions` runs once at boot (`bobi/service.py:533`) and the list is handed
to the spawn (`:663`); the reconnect path re-asserts an in-memory list rather than
re-reading config (`bobi/events/client.py` `_needs_resubscribe`).

The restart is cheaper than it sounds, and the earlier draft undersold it: the manager
session is preserved (cleared only on `--fresh`), and the saved-deployment path PUTs the
newly authorized list against the existing `deployment_id` (`bobi/subagent.py:1425-1435`)
with the event cursor intact, so events arriving during the gap replay rather than drop.

Two operator consequences the docs must state, not bury:

- A restart can strand in-flight runs. The instruction is "restart, then check the runs
  table and resume stranded runs" (`docs/RUN_RESUME.md`).
- The first restart after onboarding a busy repo is when its backlog can fan into
  auto-dispatch, because `auto_dispatch` (`package/agent.yaml:95-111`) arms
  `issue-lifecycle` and `pr-closed` with `allow_self_authored: true` for every
  subscribed repo. Expect worker launches on a newly onboarded repo.

The identity-preserving live path, named so the follow-on need not rediscover it:
`PUT /deployments/<id>/subscriptions {"replace": [...]}` (`bobi/subagent.py:1474`, route
`event-server/worker/src/index.ts:650`). `register()` is the wrong primitive - it
supersedes the deployment and mints a new `deployment_id`/`api_key`. A watcher belongs in
the manager process next to `MonitorScheduler`, must reassign `active_subscriptions`
(`:1483`) because `_resubscribe_on_deaf` replays it (`:1510`), and must not re-invoke
`_start_event_subscription`, whose `except` unlinks the event cursor (`:1435`, `:1486`).

### Authority, in one line as ruled

Write authority moves into the overlay with everything else. Branch-delete safety is
policy and prompt, not machinery: the guard is the role prompt's decline-on-human-branch
default, and nothing here enforces or weakens it.

### The companion prompt PR is load-bearing, not a footnote

Verified: `managed_repos` has zero consumers in `bobi/` **and zero in the installed pack
beyond its own definition** (`run/package/agent.yaml:125`). No role prompt, tool doc,
workflow or monitor reads it; the director's belief about write authority comes from
long-term memory.

So moving the list into the overlay changes nothing on its own. Until the prompt reads
the overlay, there are two lists and the stale one governs. The companion PR in
`moda-labs/moda-agents` is a **blocking co-deliverable**, and the prompt instruction is
specified here verbatim rather than left to interpretation:

> Repo management lives in `<run>/workspace/config.yaml`. A top-level key present there
> replaces the same key in `run/package/agent.yaml`. Read `managed_repos:` from the
> merged view (`bobi agent <name> config show`), never from `package/agent.yaml` alone.

## Migration

Write `<run>/workspace/config.yaml` once, containing the complete current `subscribe:`
list and the current `managed_repos:` list, copied from `run/package/agent.yaml` in the
pack's own order (lightweave, moda-agents, bobi-agent, moda-skills). Behaviour on day one
is unchanged.

To be precise: consumption is order-insensitive (`bobi/service.py:543`, `:549` are
membership tests), so this is a semantic no-op rather than a literal byte match.

`moda-labs/familystories-ai` appears in neither list. It was offboarded 2026-08-04
22:24 UTC (`c0ec10a` in moda-agents) because baohua is its sole engineering team.
Verified genuinely off: parsing the director's event log by `payload.repository.full_name`
over a 33-hour window shows 628 familystories events, newest `2026-08-04T21:29:36`, and
**none** after the 22:37:54 pack reinstall. No ongoing leak. (A substring grep for
`familystories` inflates this badly - the string appears inside `moda-labs/bobi-agent`
issue and PR payloads.)

A commented seed template ships in the pack's `workspace/`. This is safe under per-key
precedence: a template that defines no keys changes nothing, and it is how an operator
discovers the file and the merge rule exist. `seed_workspace()` copies it only if absent
and is otherwise untouched.

**Open question for Zach, not decided here.** `bobi-agent` and `moda-agents` are
subscribed but unmanaged today. Copying verbatim keeps them unmanaged. That is the
zero-change choice, and once the overlay is live, changing it is a one-line edit with no
PR - which is the point. Flagged rather than silently promoted.

The pack's `subscribe:` and `managed_repos:` stay in place as the seed layer for a fresh
deployment that has no overlay yet.

## Verification plan

- Unit: an overlay key replaces the pack's key; absent keys fall through; an absent
  overlay is exactly today's behaviour.
- Unit: `${VAR}` in overlay values interpolates identically to the pack.
- Unit: a malformed overlay fails manager boot loudly with file and reason, and is not
  swallowed by `subscriptions.py`'s `except Exception: pass`.
- Unit: `Config.load` reflects the overlay, so an overridden `auto_dispatch:` or
  `services:` is honoured rather than half-applied.
- Unit: `discover_subscriptions`' primary path and its `Config.load` fallback agree.
- Unit: `find_required_env_vars` over the merged document - an overlay replacing a key
  that carried `${VAR}` in the pack must not silently stop requiring that secret
  (`_build_only_names` computes `in_build - elsewhere`, so replacement can flip a var to
  build-only).
- Regression, the resurrection trap: pack `subscribe:` re-declaring an offboarded repo
  does not resubscribe it while the overlay defines `subscribe:`.
- Regression, the framework-purity constraint: no repo, `managed_repos` or `tracker`
  semantics appear under `bobi/`. This is a test because it is the constraint most
  likely to erode.
- Regression: `seed_workspace()` unchanged; the seed template defines no keys.
- Integration: a full manager start against the local event server registers exactly the
  overlay's `subscribe:` set, and a repo removed from the overlay is genuinely
  unsubscribed after restart rather than merely absent from the file.
- Integration: the #488 resource-grant path (`bobi/events/server.py:609`) authorizes
  overlay-sourced topics, and an ungranted topic is reported rather than silently dropped.

## Implementation plan

1. `bobi/agent_config.py`: `load_agent_yaml()` with replace semantics and interpolation.
2. `Config.load` uses it; `events/subscriptions.py:34` and `ingress.py:62` use it;
   `monitors/registry.py:80` gets a records-from-dict path.
3. Boot-time overlay validation in `run_manager_from_config` (`bobi/service.py:500`).
4. `bobi agent <name> config show`.
5. Commented seed template in the pack's `workspace/`.
6. Migration overlay written into the live runtime.
7. Docs: `docs/BUILDING_AGENT_TEAMS.md` gains a runtime-overlay section covering the
   merge rule, the restart consequences above, and guidance that the overlay is for
   operational keys - replacing `services:`, `build:` or identity keys is unsupported
   and will make setup and doctor disagree with the runtime. Nothing to rewrite:
   `managed_repos` does not appear in `docs/` at `origin/main`.
8. **Blocking co-deliverable:** companion PR in `moda-labs/moda-agents` with the prompt
   text above.

Complexity: the feature logic is small; the size is routing every content reader through
one loader. The earlier "ten parse sites" framing was doubly wrong - two of the ten never
parse the document, and it missed the one with 42 call sites.

## Review record

**Spec review gate.** The house binding is `/gstack-plan-eng-review`,
`/gstack-plan-design-review` and `/gstack-plan-ceo-review`. Those skills are interactive
and cannot run in a headless worker, so the three lenses ran as adversarial passes
instead - architecture/edge-cases/tests, design/operator-experience, and scope. A
labelled substitution, not a claim to have run the skills. Not plan-born (no plan
artifact referenced, no matching bracket prefix), so all three lenses apply.

**What this round changed.** The eng lens found the `Config.load` omission and that loud
failure was unreachable behind five existing exception handlers; both were verified
first-hand and the design changed - `Config` is now the chokepoint and validation moved
to boot. The design lens scored operator experience 3/10 and produced the read-back view,
the seed template, the filename change off the `ROOT_MARKER` collision, and the restart
consequences. The scope lens found the companion prompt PR was load-bearing but specified
in one line, and it is now a blocking co-deliverable with verbatim text. The `${VAR}`
rule was inverted after two lenses independently showed the stated rationale was backwards.

**Earlier rounds.** Two adversarial rounds on the pre-ruling design returned five
blockers before Zach's premise ruling replaced it entirely.

**Residual.** This revision has not itself been re-reviewed end to end; the mechanical
claims behind each change were verified first-hand instead (`ROOT_MARKER`,
`Config.load`'s 42 call sites, the `subscriptions.py:43` swallow, the `:45` fallback).

**Cross-model pass: still owed.** `codex exec` returns
`401 Unauthorized: Missing bearer or basic authentication` in this container and there is
no fallback LLM key. Same-model passes are a fallback, not a substitute.
