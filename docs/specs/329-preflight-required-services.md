## Resolved by human sign-off (Zach, 2026-06-22)

> Both open decisions were resolved by the human reviewer when approving this spec. Recorded here so they're part of the reviewed file, not just the PR thread.

1. **Behavior-preserving `required:` annotations — confirmed.** Mark genuinely-critical services `required: true` to preserve today's blocking behavior — **eng-team:** `github` / `slack` / `linear`; **support-manager + market-research:** `slack` / `linear`. **dogfood: `github` _and_ `email`/venn are both `required: true`.** Per the reviewer, the dogfood-content-review team genuinely needs Venn to function (it polls Gmail and acts on email), so `email` is **not** optional — it is required, and the dogfood team must be provisioned with real Venn credentials (operational follow-up — see [Implication of the dogfood decision](#implication-of-the-dogfood-decision) below).
2. **Cosmetic — warning presentation — confirmed.** Use the `⚠` glyph for non-blocking warnings and `✗` for blocking errors (glyphs are good for readability), with a `[WARN]` / `[ERROR]` text fallback for unicode-stripped terminals.

### Implication of the dogfood decision

The original #329 trigger was *"the credential-free dogfood/release smoke can't `modastack start` because Venn `email` isn't connected."* The reviewer resolved dogfood **the other way**: `email` is required and the dogfood team gets real Venn credentials, rather than letting dogfood's `email` degrade. So:

- The **framework mechanism** (per-service `required` flag + graceful degradation of non-required services) still ships exactly as specified below — it is the general fix and benefits any pack.
- For **dogfood specifically**, the smoke now runs **with** Venn credentials provisioned, not credential-free. The graceful-degradation path is verified against a genuinely-optional service rather than dogfood `email` (see updated Verification plan / Acceptance criteria).

---

> **Agent-authored design spec — pending human approval.** Written for #329 by the engineer agent and reviewed via plan-eng / plan-design / plan-ceo lenses. Not self-approved: routing to the director for human (project-lead → director) sign-off before implementation. Original issue text preserved at the bottom.

---

# Spec: graceful preflight degradation for unconfigured non-required services (#329)

## Problem

`modastack start` runs preflight checks before forking the agent. Today
**any** failed check blocks startup (`cli.py` `start`:
`if not validation.ok: raise SystemExit(1)`, where
`ValidationResult.ok = all(c.ok for c in checks)`). A single unconfigured
service — even a non-entry, non-critical one — bricks the whole agent.

Surfaced on the 0.22.0 dogfood run: the `dogfood-content-review` pack
declares a Venn-backed `email` service. With no `VENN_API_KEY` (and even
with a dummy one — `_check_venn_services` makes a live `POST` to venn.ai),
preflight returns `✗ email venn — not connected` and startup aborts, even
though the user only wants GitHub. The credential-free dogfood/release
smoke can't `modastack start` at all without a real Venn + Gmail
connection.

Not a regression: the all-or-nothing gate is pre-existing (auth-v1, #281,
shipped in 0.21.0). Filing as a quality/UX fix, not a release blocker.

> **Note (human review, 2026-06-22):** the *general* problem above — one
> unconfigured non-critical service bricking the whole agent — is the real
> target and is fixed by the mechanism below. For **dogfood specifically**,
> the reviewer determined `email`/venn is genuinely **required** (dogfood acts
> on email), so dogfood is fixed by provisioning Venn creds, not by degrading
> `email`. See [Implication of the dogfood decision](#implication-of-the-dogfood-decision).

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
- **`modastack doctor` consistency** (`doctor.py._check_services`, ~line
  220): thread the new `required` flag through doctor's own `CheckResult`
  so `doctor` mirrors the warn-vs-block distinction instead of reporting
  every degraded service as a hard failure.
- **Annotate critical services in shipped packs as `required: true`** (see
  Resolved decision 1) so this change does **not** silently loosen existing
  multi-service packs (eng-team, support-manager, market-research). For
  dogfood-content-review, **both `github` and `email`/venn are `required: true`**
  (per human review — dogfood genuinely needs Venn). Only services a pack
  author explicitly marks `required: false` degrade.
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

### 3a. `modastack doctor` parity (doctor.py ~line 220)
`doctor.py` defines its **own** `CheckResult` (separate class) and
`_check_services` re-wraps validate's results:
`CheckResult(c.name, ok=c.ok, detail=c.detail, hint=c.hint)`. Add a
`required: bool = True` field to doctor's `CheckResult` and pass
`required=c.required` in that copy, and render `⚠` for non-required
failures in doctor's output, so `modastack doctor` doesn't flag a
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
- **Degradation path** — `modastack start` against a pack with an
  unconfigured service explicitly marked `required: false`: preflight shows
  `✓` for required services, `⚠` for the optional one, prints "degraded mode",
  and the manager starts (satisfies acceptance criteria 1 & 2). dogfood
  `email` is **no longer** this case — it is `required: true` per human
  review, so use a pack/service marked `required: false` to exercise this.
- **dogfood smoke** — `modastack start` against `dogfood-content-review` now
  requires **both** `github` and `email`/venn; the dogfood team must have real
  Venn credentials provisioned (operational follow-up). With creds present,
  preflight shows `✓ github`, `✓ email` and the manager starts.
- `modastack start` with a genuinely required service unset still blocks
  (acceptance criterion 3) — including dogfood with no `VENN_API_KEY`.

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
   Note: dogfood `email` is `required: true` — drop the
   "email is optional / degrades" annotation that the premature `agent/329`
   impl added, and provision Venn creds for the dogfood team (operational).
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
   - **dogfood-content-review**: `github` **and** `email`/venn (human review,
     Zach 2026-06-22 — dogfood genuinely requires Venn; provision creds)

   **Confirmed by the human** (Zach, 2026-06-22). This is a deliberate
   behavior-preserving change to shipped packs. The dogfood change shifts the
   original acceptance criterion (see [Implication of the dogfood decision](#implication-of-the-dogfood-decision)):
   dogfood now starts **with** Venn creds rather than degrading `email`.

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

## Acceptance criteria (restated; dogfood criterion updated per human review)
- A pack declaring an unconfigured service marked `required: false` can
  `modastack start`; the unmet service is a **warning**, not a block.
- Entry-point / required-service failures still block (regression guard).
- **dogfood (revised by Zach, 2026-06-22):** `email`/venn is `required: true`,
  not optional. The dogfood team is provisioned with real Venn credentials and
  the manager smoke runs **with** them; dogfood with no `VENN_API_KEY` blocks
  (as it should for a required service). The original "runs credential-free"
  criterion is superseded — see [Implication of the dogfood decision](#implication-of-the-dogfood-decision).

---

