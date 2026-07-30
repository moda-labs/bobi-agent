# Issue #858: Let a Package Manager Upgrade Bobi Past Its Own Write Guard

## Problem

`bobi/runtime_guard.py` runs at every brain launch (`prepare_brain_runtime`) and
strips write bits from Bobi's own installed framework package
(`site-packages/bobi/`) and its `.dist-info`. The sweep strips write bits from
**directories** as well as files, and POSIX governs unlink/create by the
*directory's* write bit — so no package manager can replace the tree the guard
just locked.

```
$ uv tool install --force "bobi @ git+https://github.com/moda-labs/bobi-agent@v0.49.0"
Resolved 44 packages in 562ms
error: failed to remove directory `~/.local/share/uv/tools/bobi/lib`: Permission denied (os error 13)

$ bobi --version
zsh: command not found: bobi
```

The mode the guard leaves behind:

```
dr-xr-xr-x  61 …  ~/.local/share/uv/tools/bobi/lib/python3.11/site-packages/bobi
```

### The real end state is worse than the issue reports

Reproduced end-to-end against a real `uv tool install` of bobi 0.50.0 (uv
0.12.0). uv removes site-packages entry by entry and fails only when it reaches
the locked `bobi/` directory — so every writable *dependency* is already gone by
the time it stops:

```
$ bobi --version
  File ".../site-packages/bobi/cli.py", line 13, in <module>
    import click
ModuleNotFoundError: No module named 'click'
```

The operator is left with a `bobi` on `PATH` that cannot import, not merely a
missing entrypoint. **Any recovery that runs `bobi` is unavailable at exactly
the moment it is needed.** A running manager survives (already loaded), so the
team looks healthy while being unmanageable: `bobi agent … stop/status/doctor`
all fail.

This also makes the naive repair silently insufficient. From the gutted state
uv's receipt still matches, so a plain `uv tool install bobi` re-links the
executable, exits 0, and leaves an install that still cannot import. Only
`--force` rebuilds it.

`pip` degrades differently but not safely: `pip install --force-reinstall`
renames the locked tree aside (rename needs write on the *parent*, which the
guard does not touch), warns `Failed to remove contents in a temporary
directory`, and exits 0 — leaving a stale `~obi` directory in site-packages on
every upgrade.

### Severity

A self-inflicted denial of upgrade that fails in the worst possible order: the
CLI is destroyed before the failure surfaces. The only known escape is a
`chmod -R u+w` on a uv-internal directory, which is not a step anyone would
guess.

It also compounds #857: on a Node 21+ host the build gate fails *after* uv has
removed the old install, producing the same uninstalled state by a different
path.

### Related but distinct

On **v0.47.0** a forced reinstall additionally re-triggers the original #798
EACCES loop: `--force` wipes the previously built `event-server/dist/`, and
0.47.0's startup path then runs `npm install` inside the now-read-only
site-packages. v0.49.0's prebuilt immutable bundle fixed that half correctly.
This issue is only about the guard blocking the *package replacement* itself.

## Root cause

The guard has a mutation window for exactly one of its three protected roots.
`with_mutable_runtime_package()` unlocks the team-package image because Bobi
itself is the mutating actor (`bobi agents install`, compose, monitor edits).
The framework roots (`bobi-package`, `bobi-dist-info`) have no window, because
their mutating actor is an **external package manager** that Bobi cannot wrap.

The guard locks a tree whose lifecycle it does not own, and never releases it.

## Goals

- `uv tool install --force` / `uv tool upgrade` / `pipx upgrade` succeed against
  a guarded install without an unguessable manual `chmod`.
- **The already-broken operator can recover**, from any prior version, without a
  working `bobi`. This is the primary goal: everyone hitting this bug today is
  on a version that predates any command this spec adds.
- The unlock survives a **running team** long enough for the package manager to
  finish. The guard re-applies on every session, subagent spawn, and workflow
  step (`bobi/session.py`, `bobi/subagent.py`, `bobi/workflow/orchestrator.py`,
  `bobi/validate.py`), and monitor ticks land every 30s — far inside the minutes
  uv needs to resolve, build, and swap a git dependency. A bare unlock loses
  this race on any host that is actually doing work.
- #751's at-rest protection is unchanged when no upgrade is in flight: the guard
  still locks files *and* directories, and still re-applies itself
  automatically.
- An open window is **observable**, not just bounded. It must never be the case
  that the guard is off and `bobi doctor` is silent about it.
- The pre-upgrade step is discoverable *before* the operator bricks themselves.

## Non-goals

- Making same-UID chmod a hard security boundary. Already an explicit non-goal
  of #751 and unchanged here (see Security below).
- A `bobi upgrade` wrapper that owns package-manager invocation (rejected
  below).
- Supporting a framework hot-swap under live agents. The window makes the
  *package manager* succeed; it does not make swapping Bobi under a running
  manager safe. See Running teams below.
- #775 (root-owned venv files reported writable) and #857 (Node version gate) —
  distinct issues.
- Cleaning up pip's stale `~obi` residue. Diagnosed here, deferred to a
  follow-up doctor check.

## Solution

Five parts. The first two are the mechanism; the rest make it discoverable.

### 1. A release window, not a bare unlock — `bobi/runtime_guard.py`

`release_runtime_write_policy()` restores `u+w` across the framework roots only
(kinds `bobi-package`, `bobi-dist-info`). The team-package image is deliberately
excluded: it is not package-manager-owned, and no package-manager upgrade needs
it (see Rejected alternative E on why it is not unified here).

Unlocking alone is not enough, because the guard re-applies on the next brain
launch. So release also **opens a time-boxed window**.

#### The marker carries content, not just an mtime

`$BOBI_HOME/runtime-guard-released` holds JSON:

```json
{"prefix": "/…/tools/bobi", "expires_at": 1790000000.0, "opened_by": "bobi guard release", "pid": 1234}
```

Deliberately outside every protected root and outside any package-manager
prefix: it must survive the framework tree being locked, survive that tree being
half-deleted by a failed upgrade, and stay writable by `scripts/install.sh` at
the point where `bobi` no longer imports.

Content rather than a bare mtime, because mtime alone cannot express three
things this design needs:

- **Explicit expiry.** `RELEASE_WINDOW` (15 minutes) is a compile-time constant;
  carrying it implicitly in the mtime means changing it retroactively changes
  the meaning of markers already on disk.
- **Which install.** A `$BOBI_HOME` shared by two installs (a uv tool and a
  venv), or shared across hosts, must not have one release suppress the guard
  for the others. A root is covered only if `prefix` is an ancestor of, or equal
  to, that root.
- **Who opened it.** Required by the observability goal and by Security below.

`install.sh` writes the same JSON with `printf` — no harder than `touch`.

#### The open/closed predicate is bounded on both sides

```
open  ⟺  parses  ∧  expires_at > now
                  ∧  expires_at ≤ now + RELEASE_WINDOW + SKEW
                  ∧  prefix covers the root under consideration
```

The upper bound is load-bearing and was missing from the first draft of this
design. Without it a future-dated `expires_at` (or mtime) holds the guard off
**forever** while `guard status` prints a plausible time — reachable via NFS
where the server clock leads the client, a VM snapshot restore, a container on a
fast-clocked host, or a backward NTP correction. Verified: a marker one year
ahead yields a window that stays open until 2027. Anything outside the bound is
treated as closed, and `doctor` reports it as a malformed marker rather than
ignoring it.

A malformed or unparseable marker is **closed**, not open. Failing toward
"locked" keeps a corrupt file from disabling the guard.

#### apply → release ordering, and the re-verify that closes the race

`apply_runtime_write_policy` skips a covered framework root while the window is
open, and reports it under a new `GuardReport.released`. Everything else still
locks normally.

Release opens the window *before* its first chmod. That alone is not sufficient:
`apply` does a check-then-act with a wide gap (marker read, then metadata
discovery and a `sorted(rglob(...))` sweep over site-packages), so this
interleaving still loses —

```
apply:   read marker → CLOSED ─────────────────────┐
release:        open marker → chmod +w             │
apply:                                    ─────────┴─→ chmod -w   ✗ locked, window open
```

— leaving the tree locked, the window open, doctor blind, and the upgrade
failing with `Permission denied`: exactly the invisible failure this design
exists to remove. So `release_runtime_write_policy` **re-verifies after its
sweep** (`root_write_failures` per root), retries once, and reports loudly if
the tree is still locked. Cheap, and it converts a silent loss into a visible
one.

`_chmod_tree` is called non-strict here. The strictness comment on that helper
governs the *mutation window*, where a half-unlocked tree with no re-lock is the
hazard. Release has no re-lock and its whole job is recovery, so it must unlock
everything it can and report what it could not.

#### State machine

```
        ┌──────────────────────── expiry / `guard reapply` ◄─────────┐
        ▼                                                            │
    ┌────────┐   guard release / install.sh   ┌──────────┐   agent launch
    │ LOCKED │ ─────────────────────────────► │ RELEASED │ ──── skips re-lock ──┐
    └────────┘                                └──────────┘                      │
        ▲                                           │                           │
        └──── agent launch re-locks ◄───────────────┴── malformed / future-dated ┘
                                                        / prefix mismatch → treated LOCKED
```

### 2. `bobi guard` CLI group

- **`bobi guard release`** — unlocks, opens the window, prints each root, the
  expiry, **and the marker path it wrote** (the window is scoped to the
  `$BOBI_HOME` of the calling shell; an operator whose manager runs under a
  different `$BOBI_HOME` must be able to see that). Reachable with no runtime
  bound. Exits **non-zero** if any path could not be unlocked *or* if the window
  could not be opened — the documented form is `bobi guard release && uv tool
  install --force bobi`, and the `&&` must not carry an operator into an
  unprotected race after a partial failure.
- **`bobi guard reapply`** — closes the window and re-locks immediately. Exits
  non-zero if the marker could not be removed, rather than reporting a re-lock
  that leaves doctor blind until expiry.
- **`bobi guard status`** — where `bobi` imported from, marker path, window
  state and who opened it, and locked/unlocked per root. This is the only
  root-free way to answer "which bobi did I import, and is it locked":
  `bobi doctor` is popped from the top-level group and is reachable only as
  `bobi agent <name> doctor`.

`release` reports honestly when there is nothing to release, distinguishing
*editable install* / *source checkout* / *no distribution metadata* rather than
claiming success, and prints the path it imported from. `status` uses the same
three-way reason rather than collapsing them.

### 3. `scripts/install.sh` releases before installing

The installer is the recovery path, and after a failed upgrade `bobi` cannot
import — so the release here must **not** shell out to `bobi`. It asks uv for
its own tool directory, `chmod -R u+w`s the bobi tool tree, and writes the
marker directly.

`install.sh` and `paths.home_dir()` must agree on `$BOBI_HOME`. They currently
would not: bash `${BOBI_HOME:-$HOME/.bobi}` versus Python
`Path(raw).expanduser().resolve()` diverge on a relative path and on a literal
`~` in the variable. Verified: `BOBI_HOME='~/x'` resolves to `/home/u/x` in
Python and a literal `./~/x` in bash — the installer would write a marker
nothing ever reads, and the window would silently not open. A contract test
pins bash's computed default against `paths.home_dir()`.

The installer removes the marker after a successful install, so a first-time
install (or a run that fails the node gate) does not leave the guard off for 15
minutes on a host that never upgraded anything.

The installer also switches `uv tool install bobi` → `uv tool install --force
bobi`. This is what makes the one-liner an upgrade and a repair rather than only
a first install — see the gutted-state note under Problem.

### 4. Doctor

- While a window is open, framework roots are **not** reported as writable
  drift — they are writable on purpose, and flagging them would train operators
  to ignore this check on every upgrade. Suppression is filtered **by failure
  kind, not by whole root**: `root_write_failures` reports writability *and*
  `symlink escapes protected root`, and only writability is expected during a
  window. Blinding symlink-escape detection for 15 minutes would be a real
  integrity hole.
- An open window is reported as an informational anomaly naming who opened it
  and when it expires — never as silence.
- While the framework roots are locked, the detail line names the pre-upgrade
  step, surfaced where the operator still has a working CLI.

### 5. Docs — installer first, `guard release` second

README and `docs/QUICKSTART.md` gain an "Upgrading" section. **Order matters and
the obvious order is wrong.**

Leading with `bobi guard release && uv tool install --force bobi` would document
a command that does not exist on any version anyone is upgrading *from*.
Verified against shipped 0.50.0: `bobi guard` → `Usage: bobi [OPTIONS]…` (no
such command). An operator follows the doc, gets an error, shrugs, runs
`uv tool install --force` anyway, and bricks themselves — the exact #858 arc,
now with a documented step that misfires.

So:

1. **The installer one-liner first.** It is the only path that works from every
   version and every state, including a `bobi` that cannot import, because it
   does the `chmod` + marker + `--force` in shell with no working `bobi`.
2. **`bobi guard release` second, marked "0.51+"**, as the ergonomic path once
   the operator is on a version that has it.
3. **Manual `chmod -R u+w "$(uv tool dir)/bobi"` + forced reinstall** for an
   already-broken install, including why `--force` is required.

`docs/SECURITY.md` documents the window and its threat-model boundary.
`skills/bobi.md` documents the new commands and names `bobi guard status` as the
top-level fallback for operators with no configured agent.

## Running teams

Goal 3 says the unlock survives a running team; the non-goals say hot-swapping
the framework under live agents is unsupported. Both hold, but only because they
are about different things, and the first draft of this spec blurred them.

Today the guard *accidentally* prevents an unsupported operation. Making the
package manager succeed means `uv` deletes and recreates `site-packages/bobi`
under live processes. Bobi uses function-local `from bobi…` imports throughout,
so a running manager can hit `ModuleNotFoundError` in the delete/recreate gap —
reproducing the "looks healthy, is unmanageable" state the Problem section
describes, on the happy path.

Therefore `bobi guard release` **detects a running manager and warns loudly**,
naming `bobi agent <name> stop`, and the docs state stopping the team as the
supported procedure. The window exists so the upgrade is not lost to a monitor
tick, not to bless hot-swapping.

## Security

The release window is **not** a privilege boundary. #751 already states that
same-UID POSIX permissions are a practical guardrail, not a hard sandbox.

But the tempting claim — "this is just a cheaper spelling of a `chmod` an agent
could always do" — **is wrong**, and the first draft of this spec made it. A raw
same-UID `chmod -R u+w` was (a) reverted on the next agent launch and (b)
reported by `bobi doctor` as writable drift. Touching one file in `$BOBI_HOME`,
a directory every agent writes to routinely, would suppress **both** the re-lock
and the detection, for 15 minutes, renewably. That converts a self-healing,
observable bypass into a persistent, doctor-silent one. That is a real
regression in the guard's value, and it is why the design carries the
mitigations it does:

- **Bounded on both sides**, so a future-dated marker cannot hold it open
  indefinitely.
- **Never silent**: doctor reports an open window as an informational anomaly
  with `opened_by` and expiry, and symlink-escape detection is never suppressed.
- **Scoped**: to the two framework roots, and to the recorded install prefix.
  The team package image under `run/package/` stays locked throughout.
- **Self-healing**: expires with no operator action; `guard reapply` closes it
  early.

What the guard actually buys is protection against the **accidental** write — an
agent editing `site-packages/bobi/subagent.py` mid-investigation, which is the
#751 incident. That path still hits EACCES.

Managed deployments needing a real boundary should use read-only mounts or split
ownership, as `docs/SECURITY.md` already says.

## Rejected alternatives

**A. Lock files, leave directories writable.** POSIX has no "allow unlink, deny
create" bit. Writable directories let an agent `rm site-packages/bobi/cli.py`
and write a replacement — the exact #751 failure this guard exists to stop.

**B. Restore write bits at process exit.** Crashes skip it; concurrent Bobi
processes mean one exiting unlocks the tree under everyone else's live agents;
and it destroys the at-rest protection the guard exists for.

**C. `bobi upgrade` wrapper that detects the manager and runs the upgrade.**
Fragile detection across uv/pipx/pip/brew and — decisively — unusable in the
state that matters, because after a failed upgrade `bobi` cannot import.

**D. Release with no window (plain unlock).** Loses the race against a running
team: a monitor tick at 30s re-locks the tree while uv is still resolving, and
the upgrade fails again for a reason the operator cannot see. This is the caveat
that forced the window into the design.

**E. Unify `with_mutable_runtime_package` onto this cross-process window.**
Tempting: that contextmanager is in-process only, so a monitor tick calling
`prepare_brain_runtime()` can re-lock `run/package` mid-`bobi agents install`
today. That is a real pre-existing race and this mechanism would fix it — but it
is a different root, a different actor, and a different failure, and folding it
in would widen a bug fix into a refactor of the guard's mutation model.
Explicitly deferred, not overlooked.

## Operational notes

- **Homebrew** would hit the same failure (`brew upgrade` removes the Cellar
  directory) and `bobi guard release` should cover it on a default same-UID
  install — but Bobi has no brew distribution path today (`scripts/install.sh`
  is uv-only), so this is **untested and unclaimed**.
- pip's stale `~obi` residue is diagnosed above and deferred: a one-line doctor
  check for `site-packages/~obi*` belongs in a follow-up, not here.

## Verification

- **e2e, real package manager** (`tests/integration/test_uv_upgrade_guard.py`):
  builds a real wheel, `uv tool install`s it, applies the *real* guard via
  `prepare_brain_runtime`, then asserts the full arc —
  1. `uv tool install --force` fails with `Permission denied` **and** `bobi`
     stops running (proves the reported failure, including the gutted-env
     detail);
  2. the shell recovery path (`chmod` + forced reinstall) repairs it;
  3. `bobi guard release` + `uv tool install --force` succeeds.
  A second test proves the window: release, then a real agent launch, assert it
  reports the roots as `released` rather than re-locking, and the upgrade still
  succeeds. Mocking cannot substitute — the failure *is* uv's removal semantics,
  and uv and pip disagree about them.
  Add a **pip/pipx leg**: pip's mode is the *silent* one (stale `~obi`, exit 0),
  and `pipx upgrade` is named in Goals.
- **CI**: pin uv on the integration runner so the regression cannot silently
  skip itself (`pytestmark` skips without the CLI). Install it the way the
  existing `actionlint` step does — pinned download, no `curl … | sh` — rather
  than claiming that precedent while piping to a shell. **Measure the added
  wall-clock** (the test builds a real wheel, which runs `npm ci` via
  `hatch_build.py`, plus five `uv tool install` runs); if it exceeds ~5 minutes,
  gate by label or schedule rather than letting it skip by default.
- **Unit** (`tests/test_runtime_guard.py`): release restores framework roots,
  leaves the team-package image locked, reports unchmoddable paths, honest no-op
  on editable installs; **future-dated marker treated as closed**; malformed
  marker treated as closed; prefix-mismatch not covered; **a concurrent `apply`
  during release leaves the tree unlocked** (re-verify path); doctor drift
  detection **resumes after expiry**; symlink-escape still reported during a
  window; `with_mutable_runtime_package` leaves the team image locked while a
  window is open.
- **CLI** (`tests/test_cli.py`): exit codes and output for release /
  nothing-to-release / partial failure / **window-could-not-be-opened** /
  reapply / **reapply-cannot-remove-marker** / status. At least one test must
  drive the real guard rather than monkeypatching
  `release_runtime_write_policy`, so the CLI→guard seam is exercised.
- **Installer** (`tests/test_install_script.py`): release runs before
  `uv tool install`; no-op when no bobi tool tree exists; install is forced;
  marker removed after success; **bash's default `$BOBI_HOME` equals
  `paths.home_dir()`**.
- **Test isolation**: `conftest.py` does not set `BOBI_HOME`, so a guard test
  that forgets it reads the developer's real `~/.bobi/runtime-guard-released` —
  a dev who ran `bobi guard release` ten minutes ago would silently change suite
  behaviour. Add a session-scoped autouse fixture pinning it.

## Implementation plan

Sequential — `runtime_guard.py` → `cli.py` → tests all touch one module.
`install.sh` + its test is the only independent lane.

1. `bobi/runtime_guard.py`: `FRAMEWORK_KINDS`, `RELEASE_WINDOW`, JSON marker
   read/write with the bounded predicate, `ReleaseReport`,
   `release_runtime_write_policy` (with post-sweep re-verify),
   `reapply_runtime_write_policy`; window-awareness in
   `apply_runtime_write_policy` and `check_runtime_write_policy`; rename
   `_check_root` → `root_write_failures`. Inline the state diagram above the
   predicate.
2. `bobi/cli.py`: the `guard` group with `release` / `reapply` / `status`,
   including the running-manager warning and the non-zero exits.
3. `scripts/install.sh`: `release_write_guard()` before install; `$BOBI_HOME`
   resolution matching Python; marker cleanup; forced install.
4. Docs: README, `docs/QUICKSTART.md` (installer-first ordering),
   `docs/SECURITY.md`, `skills/bobi.md`.
5. Tests: unit, CLI, installer, and the uv + pip integration regressions; pin uv
   in `ci.yml`.

No `VERSION` or `CHANGELOG.md` changes (Release Rules).

## Review record

Spec reviewed 2026-07-30 by the house triple gate (`gstack-plan-eng-review`,
`gstack-plan-design-review`, `gstack-plan-ceo-review`). Scope verdict: hold the
scope, all three subcommands and the window justified. The installer-first doc
ordering (§5), the two-sided expiry bound and re-verify (§1), the corrected
Security argument, and the running-teams section are all review findings folded
back in. The codex cross-model pass did not run — the CLI is unauthenticated in
this environment.
