# The single-agent view on the hosted console

> **Status:** Draft
> **Tracking issue:** moda-labs/bobi-agent#963 · **Created:** 2026-08-05 · **Last amended:** — (see Amendments)
>
> Markers: `[ ]` idle · `[wip]` in progress · `[x]` done · `[f]` failed/blocked (always with a note)

## Purpose

Make the single-agent view from `plans/2026-07-31-single-agent-view.md` (MOD-261,
shipped in 0.55.0) work on the **hosted console** — and, in doing so, ship the
piece that lets anyone assemble a hosted console at all.

Today the view is whole only on `LocalRuntime` (`bobi app` against a local
`$BOBI_HOME`). On the hosted console it is **half a page**: what the agent is
doing right now answers, what it has *done* does not.

## Problem

Verified against `v0.55.0` on 2026-08-05, by driving the real hosted stack
(Worker + supervisor + stub manager + `EventBusRuntime`) and reading every
endpoint the page calls — not by inference from the ABC:

| endpoint | hosted | backs |
|---|---|---|
| `/health` | **200** | status strip, telemetry |
| `/status` | **200** | the page title, lifecycle polling |
| `/spend` | **200** | spend figures |
| `/overview` | **409** | the identity header's saved / about popovers |
| `/runs` | **409** | the runs table |

The lifecycle verbs (`start` / `stop` / `restart`) also work, because they are
existing admin commands. So a hosted agent can be watched and recovered today;
what is missing is its **history and composition**. Two read models, not five.

That is a smaller user-visible gap than "the page is broken", and it does not
shrink the work below — every method in the next section still has to be
implemented — but it should set the urgency honestly.

- **Seven `TeamRuntime` methods are `LocalRuntime`-only.** `runs`, `overview`,
  `run_details`, `transcript`, `resume_run`, `remind_run`, `close_run` are
  deliberately **not** `@abstractmethod` — the base implementations
  `raise TeamLifecycleError("… not available on this runtime")`, which
  `webapp/server.py:144` maps to **HTTP 409**.
- **The routes register unconditionally**, so the console renders the new page
  and the two calls above 409. The page degrades rather than crashing — it
  renders "Could not read this agent's runs" and an empty saved chip — which is
  why this was invisible until someone drove it. Confirmed live: the deployed
  console serves #948's `static/views/agent.js`.
- **`EventBusRuntime` cannot reach a filesystem.** Every read goes through
  `_command(fleet, instance, command, args)` to the operator-authed `/fleet`
  route, which speaks the sidecar admin protocol. That protocol's **entire read
  vocabulary** is `status`, `transcript`, `roster`, `spend`, `session_log`
  (`bobi/supervisor/admin.py:55`), plus `chat` / `start` / `stop` / `restart`.
  Nothing returns runs, overview, or run details, and there is **no run-scoped
  write at all**.

### The structural problem underneath it

**This repo ships the sidecar (`bobi/supervisor/`), the Cloudflare event bus
(`event-server/worker/`), and the console UI (`bobi/webapp/`) — and withholds
the ~470 lines that connect them.** Someone can self-host every published piece
and still not assemble a hosted console, because the runtime that drives one
lives in `moda-agents`.

That placement does not survive inspection.
`bobi-deploy/bobi_deploy/src/bobi_deploy/webapp/runtime.py` imports **only**
stdlib, `httpx`, and the **public** `bobi.webapp.runtime`. It mentions
`bobi_deploy` exactly once, in a docstring naming its binder — not an import.
It contains **zero** Moda-specific literals: no fleet name, no URL, no team
names. Everything situational arrives through the constructor
(`fleet_api_url`, `operator_token`).

It is generic framework code sitting on the private side of the line. Every
contract it depends on — the `/fleet` routes, the admin protocol, the ABC — is
already public. Only the class implementing them is not.

**What is genuinely Moda's is the binding, not the mechanism:**
`bobi_deploy/webapp/hosted.py:46`, which constructs it with Moda's URL and
operator token. That stays private, along with the Fly deploy engine and the
fleet configs.

## The thing that makes the rest small

The read builders are **already pure functions over a filesystem root**:

| builder | signature |
|---|---|
| `bobi/webapp/runs.py` | `build_runs(root, *, manager_name, status, query, offset, limit, now)` |
| `bobi/webapp/overview.py` | `build_overview(root)` |
| `bobi/webapp/details.py` | `build_details(root, run_id)` |

`LocalRuntime` is a thin wrapper — resolve the agent root, call the builder.
**The supervisor runs on the box, where that root exists**, so the new admin
commands are equally thin delegates to the *same* builders. No read logic is
re-implemented, and the two runtimes cannot drift, because they call one
function.

## Non-goals

- **No new UI.** The page shipped in 0.55.0; this makes its calls answer on a
  second runtime.
- **No widening of operator authority.** New commands ride the existing
  `FLEET_OPERATOR_TOKEN` gate and command envelope — more verbs, not more power.
- **No change to `LocalRuntime`'s behavior.**
- **Moda's deployment surface stays private** — the Fly engine, fleet configs,
  and the hosted app's binding. Only the generic adapter moves.

## Design

Repo shorthand: **`bobi-agent`** is this repo (public, the framework).
**`moda-agents`** is the fleet repo (private), which consumes released `bobi`
versions and carries the deploy engine as its `bobi-deploy/` subtree. The
standalone `moda-labs/bobi-deploy` repo is **archived** — not a lane, not a
location, not somewhere to open a PR.

### Lane A — move `EventBusRuntime` into the framework (`bobi-agent`)

A **pure move**, reviewable as one: 471 lines of implementation and 567 lines of
tests, from `moda-agents` into `bobi/webapp/` and `tests/`. No behavior change,
no signature change. The tests come with it and must pass **unmodified** — that
is the proof it was generic all along.

Lands as `bobi/webapp/event_bus.py` (its own module) rather than appended to
`runtime.py`, which is already 973 lines.

Worth doing **first and alone**, because everything after it becomes
single-repo work.

### Lane B — the vocabulary and the implementation (`bobi-agent`)

Now one repo, one CI, both implementers visible — so this can be a single PR.

Six new commands in `bobi/supervisor/admin.py`, each a thin delegate:

| command | args | result | delegates to |
|---|---|---|---|
| `runs` | `{status, query, offset, limit}` (all optional) | `{"runs": [...], "counts": {...}, "total", "offset", "limit", "query", "truncated"}` | `build_runs` |
| `overview` | — | `{"overview": {...}}` | `build_overview` |
| `run_details` | `{"run_id"}` | `{"details": {...}}` | `build_details` |
| `resume_run` | `{"run_id"}` | `{"ok", "accepted", "run_id", "workflow", "await_event"}` | the path `LocalRuntime.resume_run` uses |
| `remind_run` | `{"run_id"}` | as `LocalRuntime.remind_run` | ditto |
| `close_run` | `{"run_id"}` | as `LocalRuntime.close_run` | ditto |

`transcript` is **not** new — the existing command already returns messages. The
ABC's `transcript()` returns a dict where `messages()` returns a list, so the
adapter reshapes rather than adding a seventh command.

- `docs/ADMIN_PROTOCOL.md` documents all six. **Additive only**, per its own
  compatibility promise; bump `SUPERVISOR_VERSION` (`snapshot.py:28`) minor.
- The three writes are the protocol's **first run-scoped writes**, and take the
  honesty discipline `chat` already has: `resume_run` returns once the resume is
  *under way* (`accepted`), never holding a request open for a workflow.
- `EventBusRuntime` implements the seven, each a `_command(...)` plus unwrap.
  `runs` and `overview` are heavier than `status`, so they need their own
  command timeouts rather than borrowing the default.
- An older supervisor rejects these as unknown commands. Render that as
  "unavailable on this instance", naming the instance — **not** a crash. The
  fleet is not uniform mid-roll.
- **Flip the seven to `@abstractmethod` and delete the fallbacks in this same
  PR.** This is the dividend of Lane A: both implementers are now under one CI,
  so the ABC's sequencing rule — which existed *only* because the second
  implementer was invisible — no longer applies.
- Correct `runtime.py:238` and `:354`, which name the archived `bobi-deploy`
  repo. As shipped in 0.55.0 they send implementors to a dead repository; after
  Lane A they are simply wrong, since the subclass is in-tree.

### Lane C — re-point the binding (`moda-agents`)

Small, and strictly after a release carrying Lanes A+B, because `moda-agents`
pins a **released** `bobi`:

- `bobi_deploy/webapp/hosted.py` imports `EventBusRuntime` from
  `bobi.webapp.event_bus`.
- Delete the moved implementation and tests.
- Re-expand
  `bobi_deploy/tests/test_hosted_webapp_ui.py::test_drive_deployed_team_from_the_browser`
  onto the runs table.

  It is **green today** and needs no repair: it was red on the `dev` channel
  from 2026-08-05 (first observed on moda-agents#100) because 0.55.0's page
  replaced the layout it asserted, and moda-agents#102 re-pointed it. That
  change is the baseline this lane builds on, so read it before touching this:

  - The **roster** and **chat** legs were deleted, not re-pointed — those
    features are gone from the product ("chat lives in Slack and the CLI"), so
    they are never coming back and this lane must not try to restore them.
  - The **lifecycle** leg survives and is now asserted more strictly (the busy
    transition, then its reversal), because `restart` / `status` are existing
    admin commands that already work here.
  - The **runs** leg is the only one left out, and only because `/runs` 409s.
    Restoring it is this lane's job, and it is the sole reason this test is
    narrower than the page.

## Order

Lane A → Lane B → release → Lane C.

Note what changed. The old ordering was a **hard constraint enforced by nothing**
— a public ABC whose only other implementer sat where CI could not see it, with
a docstring asking people to remember. After Lane A that constraint is
*half* dissolved: the in-tree `EventBusRuntime` is now covered by this repo's
CI, so the ABC and its implementer can no longer drift unnoticed here.

**Corrected 2026-08-05 while building Lane A — the other half does not
dissolve until Lane C, and this changes B7.** `moda-agents` keeps its own
copy of `EventBusRuntime` until Lane C re-points the binding, and its
`deploy-package.yml` installs the public repo from a **sibling checkout at
`dev`** (`.github/actions/setup-public-bobi`, ref defaults to `dev`, which
this repo's promote-dev job fast-forwards to every green main). So B7's
`@abstractmethod` flip still reds that repo the moment it merges — Python
refuses to instantiate the private copy once the seven are abstract, and its
33 `EventBusRuntime` tests plus `hosted.py` all instantiate it. That is
precisely the breakage the ABC's sequencing rule exists to prevent, so the
flip is **not** yet "protected by CI instead of prose".

**Zach's call, 2026-08-05: accept the red canary.** The order stands —
`A → B → release → C` — B7 stays inside Lane B, and `moda-agents`'
`deploy-package.yml` goes red from B7's merge until Lane C lands. The
alternatives were both worse: moving Lane C ahead of B7 makes it consume an
*unreleased* `bobi`, which is the constraint that put it last in the first
place; splitting B7 into a fourth step buys a clean window at the cost of an
extra PR and a flip stranded past a release.

Two obligations follow from choosing this, and **Lane B owns both**:

- **Lane B's PR must state that the canary will go red, and why**, before it
  merges — a knowingly-red check that nobody announced is indistinguishable
  from a regression, and the next person to look pays for it. `moda-agents`
  is a consumer; it never gates this repo's main.
- **Lane C is the repair, and follows the release promptly.** The length of
  that window is the entire cost being accepted here, so it is short by
  intent, not by luck.

Lane A is unaffected either way — it adds a module and breaks nothing.

## Validation gates

<!-- checklist -->

- [x] **A1** `EventBusRuntime` and its tests live in `bobi-agent`; the moved tests pass with **no change to any assertion, fixture, or test body** — the import path is the only edit, and it must change, so "unmodified" was never literally achievable.
- [x] **A2** It still imports nothing private — asserted by `tests/test_import_boundaries.py`, so a future private import fails CI rather than re-splitting the seam.
- [ ] **B1** Six commands in `ADMIN_COMMANDS` and the dispatch chain, each delegating to the existing builder — no re-implemented read logic.
- [ ] **B2** `docs/ADMIN_PROTOCOL.md` documents all six; `SUPERVISOR_VERSION` bumped; the additive-only promise holds.
- [ ] **B3** Each read command's payload is **identical** to `LocalRuntime`'s for the same root — the anti-drift gate, and the reason the delegate design was chosen.
- [ ] **B4** The three writes are proven against a real suspended workflow run, not a fixture: resume moves the run, and two concurrent resumes do not double-run it.
- [ ] **B5** An unknown `run_id` and a non-resumable run return the documented error shapes.
- [ ] **B6** A supervisor that does not know the command surfaces as unavailable, naming the instance — asserted, not assumed.
- [ ] **B7** The seven are `@abstractmethod`, the fallbacks are gone, and **both** runtimes satisfy the ABC in one CI run.
- [ ] **B8** No file under `bobi/` names the archived `bobi-deploy` **repo** as a place a subclass lives — grep gate, so it cannot silently return.
- [ ] **C1** `hosted.py` binds the framework class; the private copies are deleted; no duplicate implementation survives.
- [ ] **C2** The hosted console's single-agent page renders with real data end to end against a live instance: `/overview` and `/runs` answer 200 where they answered 409, and `test_drive_deployed_team_from_the_browser` is re-expanded to drive the runs table (it is green before this lane starts — moda-agents#102 — so "still green" proves nothing here; the gate is the restored leg, and the roster/chat legs stay deleted).

## Amendments

_None yet._
