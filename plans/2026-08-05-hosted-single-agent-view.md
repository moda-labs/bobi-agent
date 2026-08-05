# The single-agent view on the hosted console

> **Status:** Draft
> **Tracking issue:** moda-labs/bobi-agent#963 · **Created:** 2026-08-05 · **Last amended:** — (see Amendments)
>
> Markers: `[ ]` idle · `[wip]` in progress · `[x]` done · `[f]` failed/blocked (always with a note)

## Purpose

Make the single-agent view from `plans/2026-07-31-single-agent-view.md` (MOD-261,
shipped in 0.55.0) actually work on the **hosted console**, which is where the
fleet is administered from.

Today it works only on `LocalRuntime` — `bobi app` against a local
`$BOBI_HOME`. On the hosted console every panel on the page errors.

## Problem

Verified against `v0.55.0` and the deployed console on 2026-08-05:

- **Seven `TeamRuntime` methods are `LocalRuntime`-only.** `runs`, `overview`,
  `run_details`, `transcript`, `resume_run`, `remind_run`, `close_run` are
  deliberately **not** `@abstractmethod` — the base implementations
  `raise TeamLifecycleError("… not available on this runtime")`, which
  `webapp/server.py:144` maps to **HTTP 409**. This was correct: the ABC's own
  docstring carries the sequencing rule (adding an `@abstractmethod` breaks
  `moda-agents`' CI the moment it merges), and each says in as many words
  *"This becomes abstract once the hosted runtime implements it."*
- **The routes register unconditionally.** `server.py` wires
  `/api/agents/{name}/runs|overview|details|…` regardless of runtime, so the
  hosted console renders the new page and every data call 409s. Confirmed live:
  the deployed console serves #948's `static/views/agent.js`.
- **`EventBusRuntime` cannot reach a filesystem.** It is a control-plane client
  living in the fleet repo, `moda-labs/moda-agents`, at
  `bobi-deploy/bobi_deploy/src/bobi_deploy/webapp/runtime.py`. Every read goes
  through `_command(fleet, instance, command, args)` to the operator-authed
  `/fleet` route, which speaks the sidecar admin protocol. That protocol's
  **entire read vocabulary** is `status`, `transcript`, `roster`, `spend`,
  `session_log` (`bobi/supervisor/admin.py:55`), plus
  `chat` / `start` / `stop` / `restart`. There is no command that returns runs,
  overview, or run details, and no run-scoped write at all.
- So `moda-agents` **cannot** implement these seven alone. The supervisor in
  this repo has to grow the vocabulary first.
- **The ABC's own docstrings name a repo that no longer exists.**
  `bobi/webapp/runtime.py:238` and `:354` (as shipped in 0.55.0) say the
  subclass lives in "the private **bobi-deploy** repo". `moda-labs/bobi-deploy`
  was **archived** in the repo reorg; the engine is now the `bobi-deploy/`
  *subtree* of `moda-agents`, and nothing builds or releases from the old repo.
  An implementor following those docstrings today goes to an archived
  repository. Fixed in Lane A, where the same docstrings are being edited anyway.

## The thing that makes this small

The read builders are **already pure functions over a filesystem root**, in the
public repo:

| builder | signature |
|---|---|
| `bobi/webapp/runs.py` | `build_runs(root, *, manager_name, status, query, offset, limit)` |
| `bobi/webapp/overview.py` | `build_overview(root)` |
| `bobi/webapp/details.py` | `build_details(root, run_id)` |

`LocalRuntime` is a thin wrapper — resolve the agent root, call the builder.
**The supervisor runs on the box, where that root exists**, so the new admin
commands are equally thin delegates to the *same* builders. No read logic is
re-implemented, and the two runtimes cannot drift, because they call one
function.

This is the same discipline the ABC docstring already demands ("both runtimes
must emit it identically, it is rendered once") — here it is enforced by
construction rather than by review.

## Non-goals

- **No new UI.** The page shipped in 0.55.0; this makes its existing calls
  answer on a second runtime.
- **No widening of operator authority.** The new commands ride the existing
  `FLEET_OPERATOR_TOKEN` gate and the existing command envelope. Same authority,
  more verbs.
- **No change to `LocalRuntime`'s behavior.**

## Design

Repo shorthand used below, since the reorg moved things: **`bobi-agent`** is this
repo (public, the framework). **`moda-agents`** is the fleet repo (private),
which consumes released `bobi` versions and carries the deploy engine as its
`bobi-deploy/` subtree. The standalone `moda-labs/bobi-deploy` repo is
**archived** — it is not a lane, a location, or a thing to open a PR against.

### Lane A — admin-protocol vocabulary (`bobi-agent`)

Six new commands in `bobi/supervisor/admin.py`, each a thin delegate:

| command | args | result | delegates to |
|---|---|---|---|
| `runs` | `{status, query, offset, limit}` (all optional) | `{"runs": [...], "counts": {...}, "total", "offset", "limit", "query", "truncated"}` | `build_runs` |
| `overview` | — | `{"overview": {...}}` | `build_overview` |
| `run_details` | `{"run_id"}` | `{"details": {...}}` | `build_details` |
| `resume_run` | `{"run_id"}` | `{"ok", "accepted", "run_id", "workflow", "await_event"}` | the same path `LocalRuntime.resume_run` uses |
| `remind_run` | `{"run_id"}` | as `LocalRuntime.remind_run` | ditto |
| `close_run` | `{"run_id"}` | as `LocalRuntime.close_run` | ditto |

`transcript` is **not** new — the existing command already returns messages. The
ABC's new `transcript()` returns a dict where `messages()` returns a list, so
Lane B adapts the shape client-side rather than adding a seventh command.

- `docs/ADMIN_PROTOCOL.md` documents all six. **Additive only**, per its own
  compatibility promise; bump `SUPERVISOR_VERSION` (`snapshot.py:28`) minor.
- The three write commands are the protocol's **first run-scoped writes**. They
  take the same honesty discipline `chat` already has: `resume_run` returns once
  the resume is *under way* (`accepted`), never holding a request open for a
  workflow. Unknown run → the error the ABC specifies (404 shape); non-resumable
  → 409 shape.
- An older supervisor will reject these as unknown commands. That is the correct
  behavior and Lane B must render it as "unavailable on this instance", not as a
  crash — the fleet is not always uniform mid-roll.

Lane A also corrects the two docstrings above (`runtime.py:238`, `:354`) to name
`moda-agents` rather than the archived `bobi-deploy` repo. They are being edited
in this lane regardless, and leaving them would keep sending implementors to a
dead repository.

### Lane B — `EventBusRuntime` (`moda-agents`)

`bobi-deploy/bobi_deploy/src/bobi_deploy/webapp/runtime.py` — the deploy
engine's subtree inside the fleet repo, not a separate checkout.

Implement the seven methods, each a `_command(...)` call plus unwrap. Mirrors
the existing `session_log` / `spend` methods exactly, including their timeout
handling — `runs` and `overview` are heavier than `status`, so they need their
own configurable command timeouts rather than borrowing the default.

Handle the mixed-version case explicitly: an instance whose supervisor predates
Lane A must surface as unavailable, with the instance named.

`bobi_deploy/tests/test_hosted_webapp_ui.py::test_drive_deployed_team_from_the_browser`
is **already failing** on the `dev` channel because 0.55.0's page replaced the
layout it asserts (first observed 2026-08-05 on moda-agents#100). It is this
lane's to repair — it is the only test that drives the hosted page end to end,
and it cannot pass until these methods answer.

### Lane C — close the seam

Once Lane B is released and deployed, flip the seven to `@abstractmethod` and
delete the `TeamLifecycleError` fallbacks. **Strictly after Lane B ships** — the
ABC's sequencing rule is not advisory: it breaks `moda-agents`' CI on merge.

## Order

Lane A → Lane B → (release + deploy) → Lane C. The ordering is a hard
constraint, not a preference.

## Validation gates

<!-- checklist -->

- [ ] **A1** Six commands in `ADMIN_COMMANDS` and the dispatch chain, each delegating to the existing builder — no re-implemented read logic.
- [ ] **A2** `docs/ADMIN_PROTOCOL.md` documents all six; `SUPERVISOR_VERSION` bumped; the additive-only promise is not broken.
- [ ] **A2b** No file under `bobi/` names the archived `bobi-deploy` **repo** as a place a subclass lives — `runtime.py:238` and `:354` corrected to `moda-agents`, asserted by a grep gate so it cannot silently return.
- [ ] **A3** A test asserts each read command's payload is **identical** to what `LocalRuntime` returns for the same root — the anti-drift gate.
- [ ] **A4** The three writes are proven against a real suspended workflow run, not a fixture: resume moves the run, a second concurrent resume does not double-run it.
- [ ] **A5** An unknown `run_id` and a non-resumable run return the documented error shapes, asserted.
- [ ] **B1** All seven `EventBusRuntime` methods implemented; `test_eventbus_runtime.py` covers each against a faked control plane.
- [ ] **B2** A supervisor that does not know the command surfaces as unavailable naming the instance — asserted, not assumed.
- [ ] **B3** The hosted console's single-agent page renders with real data end to end against a live instance; every panel that 409'd now answers.
- [ ] **C1** The seven are `@abstractmethod`; the fallbacks are deleted; both runtimes still satisfy the ABC.
- [ ] **C2** Verified that no deployed console predates Lane B before C1 merges.

## Amendments

_None yet._
