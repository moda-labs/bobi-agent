## Resolved by human sign-off (Zach, 2026-06-22)

> Both open decisions were resolved by the human reviewer when approving this spec. Recorded here so they're part of the reviewed file, not just the PR thread.

1. **Behavior-preserving `required:` annotations — confirmed.** Mark genuinely-critical services `required: true` to preserve today's blocking behavior — **eng-team:** `github` / `slack` / `linear`; **support-manager + market-research:** `slack` / `linear`. **dogfood-content-review: only `github` is `required: true`; `email`/venn is `required: false` (optional, degrades gracefully).** Per the reviewer (Zach, 2026-06-22 — changed from the earlier "venn required" call), Venn is an **optional dependency for dogfood**: a missing Venn credential degrades email events rather than blocking startup, so the credential-free dogfood/release smoke can start with GitHub alone. dogfood `email` is therefore the canonical live example of the graceful-degradation path this spec ships.
2. **Cosmetic — warning presentation — confirmed.** Use the `⚠` glyph for non-blocking warnings and `✗` for blocking errors (glyphs are good for readability), with a `[WARN]` / `[ERROR]` text fallback for unicode-stripped terminals.

---

> **Agent-authored design spec — pending human approval.** Written for #329 by the engineer agent and reviewed via plan-eng / plan-design / plan-ceo lenses. Not self-approved: routing to the director for human (project-lead → director) sign-off before implementation. Original issue text preserved at the bottom.

---

# Spec: graceful preflight degradation for unconfigured non-required services (#329)

## Problem

`bobi agent <name> start` runs preflight checks before forking the agent. Today
**any** failed check blocks startup (`cli.py` `start`:
`if not validation.ok: raise SystemExit(1)`, where
`ValidationResult.ok = all(c.ok for c in checks)`). A single unconfigured
service — even a non-entry, non-critical one — bricks the whole agent.

Surfaced on the 0.22.0 dogfood run: the `dogfood-content-review` pack
declares a Venn-backed `email` service. With no `VENN_API_KEY` (and even
with a dummy one — `_check_venn_services` makes a live `POST` to venn.ai),
preflight returns `✗ email venn — not connected` and startup aborts, even
though the user only wants GitHub. The credential-free dogfood/release
smoke can't `bobi agent <name> start` at all without a real Venn + Gmail
connection.

Not a regression: the all-or-nothing gate is pre-existing (auth-v1, #281,
shipped in 0.21.0). Filing as a quality/UX fix, not a release blocker.

> **Note (human review, 2026-06-22):** the *general* problem above — one
> unconfigured non-critical service bricking the whole agent — is the real
> target and is fixed by the mechanism below. For **dogfood specifically**,
> the reviewer (Zach) determined `email`/venn is an **optional** dependency:
> a missing Venn credential degrades email events gracefully so the
> credential-free dogfood/release smoke can `bobi agent <name> start` with GitHub
> alone. dogfood `email` (`required: false`) is the canonical example
> exercising the degradation path.

## Solution

Make preflight **fail-fast only on the entry point and genuinely required
services; warn (not block) on optional/secondary services.** A missing
non-critical service degrades the agent (e.g. GitHub works; email events
just don't arrive until Venn is connected) instead of bricking startup.

Implements **option (1)** from the issue: a per-service `required: true|false`
(default `false`) flag in `agent.yaml`. Preflight blocks only on
required-service failures plus the entry-point check; every other failed
service check becomes a warning and startup proceeds in degraded mode.

Option (2) ("block only on the entry point's own dependencies") is
rejected: `entry_point` is a **role** name, and the config has no mapping
from a role to the services it depends on, so there is no clean data model
for "the entry point's dependencies." Option (1) gives pack authors
explicit, declarative control.

## Scope

### In scope
- Add `required: bool = False` to `ServiceConfig` (config.py) + parse it.
- Add a per-check severity to `CheckResult` (validate.py) so a failed check
  can be a warning rather than an error.
- Service checks (`_check_service_credentials`, `_check_venn_services`)
  inherit each service's `required` flag — non-required failures warn.
- `ValidationResult.ok` blocks only on **required** failed checks.
- `format()` renders required failures as errors (`✗`) and non-required
  failures as warnings (`⚠`).
- `start` (cli.py) prints a clear "starting in degraded mode" note (to
  stderr) **only when it actually proceeds** past non-required failures.
- **`bobi agent <name> doctor` consistency** (`doctor.py._check_services`, ~line
  220): thread the new `required` flag through doctor's own `CheckResult`
  so `doctor` mirrors the warn-vs-block distinction instead of reporting
  every degraded service as a hard failure.
- **Annotate critical services in shipped packs as `required: true`** (see
  Resolved decision 1) so this change does **not** silently loosen existing
  multi-service packs (eng-team, support-manager, market-research). For
  dogfood-content-review, **only `github` is `required: true`; `email`/venn
  is `required: false`** (per human review — Venn is optional for dogfood and
  degrades gracefully). Only services a pack author explicitly marks
  `required: false` (or leaves unmarked) degrade.
- Unit tests for every new branch; keep entry-point/required-failure
  blocking as a regression guard.

### Out of scope
- MCP server checks (`_check_mcp_servers`) keep blocking. MCP servers are
  explicitly-configured infra; a connection failure is almost always a real
  misconfig, and the dogfood pack declares none, so it is outside the
  acceptance criteria. (Revisit separately if needed.)
- The entry-point role check stays a hard block.
- No change to *what* the venn/credential checks probe — only to whether a
  failure blocks.
- No new CLI flag (`--skip-preflight` / `--allow-degraded`, option 3 in the
  issue) — the per-service flag is declarative and strictly better.

## Technical approach

### 1. `ServiceConfig.required` (config.py)
```python
@dataclass
class ServiceConfig:
    name: str
    events: bool = False
    required: bool = False          # NEW
    credentials: dict[str, str] = field(default_factory=dict)
    channels: list[str] = field(default_factory=list)
```
Parse in `Config._parse`: dict-form services read
`required=bool(s.get("required", False))`; string-form services
(`- github`) default to `False`. (String-form already implies a
zero-config native service, so non-required is the right default.)

### 2. `CheckResult.required` (validate.py)
```python
@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""
    hint: str = ""
    required: bool = True           # NEW — default True preserves blocking
```
`required` defaults to `True` so existing call sites (entry-point, MCP)
keep blocking with no change. Only the two service-check functions thread
through each service's actual `required` value.

`required` is only meaningful when `ok is False`: it decides whether a
failure blocks (`required=True`) or warns (`required=False`).

### 3. Service checks thread the flag
- `_check_service_credentials`: on the missing-creds branch, build the
  failing `CheckResult` with `required=svc.required` (the loop already has
  `svc` in hand).
- `_check_venn_services`: `check_services()` returns only `connected` /
  `missing` **name strings**, so first build a name→ServiceConfig map from
  `venn_services`, then apply `required` per service:
  ```python
  by_name = {s.name: s for s in venn_services}
  ...
  # no-API-key branch:
  required=s.required          # already iterating ServiceConfig `s`
  # live-missing branch:
  required=by_name[name].required
  ```

### 3a. `bobi agent <name> doctor` parity (doctor.py ~line 220)
`doctor.py` defines its **own** `CheckResult` (separate class) and
`_check_services` re-wraps validate's results:
`CheckResult(c.name, ok=c.ok, detail=c.detail, hint=c.hint)`. Add a
`required: bool = True` field to doctor's `CheckResult` and pass
`required=c.required` in that copy, and render `⚠` for non-required
failures in doctor's output, so `bobi agent <name> doctor` doesn't flag a
perfectly-startable pack's optional service as a failure.

### 4. `ValidationResult.ok` — block only on required failures
```python
ok=not any((not c.ok) and c.required for c in checks)
```
Equivalently: pass unless a *required* check failed.

### 5. `format()` — warn vs error icons
```python
if c.ok:        icon = "✓"
elif c.required: icon = "✗"   # blocking
else:           icon = "⚠"   # warning, non-blocking
```
Hints still print for any failed check.

### 6. `start` (cli.py) — announce degraded mode
After printing the preflight table, keep the existing
`if not validation.ok: ... raise SystemExit(1)` block (required failures
still abort with the "Startup blocked" message — these are never relabeled
"degraded"). **Only on the proceeding path** (`validation.ok is True`),
if there are non-required failures
(`[c for c in validation.checks if not c.ok and not c.required]`), emit a
one-line notice to **stderr**:
`"Starting in degraded mode — optional services unavailable until configured: <names>."`
This guarantees "degraded mode" is shown only when we actually start, never
alongside a block.

## Verification plan

Unit tests (`tests/test_validate.py`, mirroring existing structure):
- `ServiceConfig` parse: `required: true` round-trips; default is `False`;
  string-form service defaults `False`.
- `_check_service_credentials`: a non-required native service with missing
  creds yields `ok=False, required=False`; a `required: true` one yields
  `ok=False, required=True`.
- `_check_venn_services`: non-required venn service with no API key →
  `ok=False, required=False`; with `required: true` → `required=True`;
  same for the live-`missing` branch (mock `check_services`).
- `ValidationResult.ok`: True when the only failures are non-required;
  False when any required check fails (entry-point or required service).
- `format()`: renders `⚠` for non-required failures, `✗` for required.
- Regression guard: entry-point failure and required-service failure still
  produce `ok=False`.

Manual / smoke:
- **Degradation path / dogfood smoke** — `bobi agent <name> start` against
  `dogfood-content-review` with no `VENN_API_KEY`: preflight shows `✓ github`
  (required), `⚠ email` (optional venn — not connected), prints the
  "degraded mode" notice, and the manager starts. Email events just don't
  arrive until Venn is connected. This is the canonical credential-free
  smoke and satisfies acceptance criteria 1 & 2.
- **Required-failure block** — `bobi agent <name> start` against a pack with a
  *required* service unset (e.g. eng-team with no `GITHUB_TOKEN`, or dogfood
  with no GitHub) still blocks with `✗` and a "Startup blocked" message
  (acceptance criterion 3 / regression guard).

Run `pytest tests/ --ignore=tests/integration/` (zero new failures).

## Implementation plan

1. Branch `agent/329` off `main`. (TDD: red tests first.)
2. Add `required` to `ServiceConfig` + parse (config.py).
3. Add `required` to `CheckResult`; update `ValidationResult.ok` +
   `format()` and `errors` docstring (validate.py).
4. Thread `svc.required` through `_check_service_credentials` and
   `_check_venn_services` (with the name→ServiceConfig map).
5. Add the degraded-mode notice in `cli.py` `start` (stderr, proceeding
   path only).
6. Thread `required` through `doctor.py`'s `CheckResult` + `_check_services`
   render.
7. Annotate shipped packs (`agents/eng-team`, `support-manager`,
   `market-research`, `dogfood-content-review`) per Resolved decision 1.
   dogfood `github` is `required: true`; dogfood `email`/venn is
   `required: false` (optional — degrades gracefully when Venn is
   unconfigured).
8. Tests for all new branches; run unit suite; `/review`.
9. Open PR against `main`: `[#329] fix: preflight degrades on unconfigured
   non-required services`.

## Resolved by triple review (flagged for human confirmation)

1. **Default `required: false` would silently loosen existing packs.** All
   three reviewers (eng / design / CEO) independently flagged that with a
   global default-false, a missing `SLACK_BOT_TOKEN` in eng-team would warn
   + start degraded instead of blocking — inverting "fail fast" into "start,
   then crash mid-task." **Resolution: fold the pack annotations into this
   same PR** so shipped packs keep today's hard-block on their essential
   services. Default-false remains correct for new/experimental packs.
   Concretely mark `required: true` on:
   - **eng-team**: `github`, `slack`, `linear`
   - **support-manager**: `slack`, `linear`
   - **market-research**: `slack`, `linear`
   - **dogfood-content-review**: `github` only. `email`/venn stays
     `required: false` (human review, Zach 2026-06-22 — Venn is optional for
     dogfood; it degrades gracefully so the credential-free smoke still starts)

   **Confirmed by the human** (Zach, 2026-06-22). This is a deliberate
   behavior-preserving change to the multi-service packs that genuinely need
   their services; dogfood `email` stays optional and is the live example of
   the degradation path.

## Resolved cosmetic decision

1. **Warning glyph — confirmed** (Zach, 2026-06-22): use `⚠` for non-blocking
   warnings and `✗` for blocking errors (glyphs are good for readability),
   with a `[WARN]`/`[ERROR]` text fallback for unicode-stripped terminals.

## Triple-review summary
- **Eng review** — approve-with-changes. Folded in: `doctor.py` parity
  (it re-wraps validate's `CheckResult` and would drop `required`); explicit
  name→ServiceConfig map for the venn `missing` branch; degraded notice only
  on the proceeding path; `CheckResult.required` defaults `True` to preserve
  entry-point/MCP blocking; note that `ValidationResult.errors` now returns
  all failures (clarify its docstring).
- **Design review** — degraded UX clear but must not read as "fully working":
  print to stderr, label services optional, only show when proceeding.
  Adopted. Glyph fallback left as cosmetic open item.
- **CEO review** — right-sized; single biggest risk = silent degrade of
  production packs → fold pack annotations into this PR. Adopted (Resolved 1).

## Acceptance criteria
- A pack declaring an unconfigured service marked `required: false` can
  `bobi agent <name> start`; the unmet service is a **warning**, not a block.
- Entry-point / required-service failures still block (regression guard).
- **dogfood (Zach, 2026-06-22):** `email`/venn is `required: false` —
  optional. `bobi agent <name> start` against `dogfood-content-review` with no
  `VENN_API_KEY` starts in degraded mode (`✓ github`, `⚠ email`); email
  events don't arrive until Venn is connected. The credential-free dogfood/
  release smoke works again — the original #329 goal.

---

