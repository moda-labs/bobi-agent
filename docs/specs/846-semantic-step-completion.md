# Issue #846: Workflow Step Completion Is Structural, Not Semantic

## Problem

The workflow engine decides a prompt step succeeded by checking that the handoff
file contains the required **keys**.
It never looks at the **values**.

`_validate_handoff` (`bobi/workflow/orchestrator.py:1211-1213`) is the whole of it:

```python
def _validate_handoff(step: StepDef, handoff: dict) -> list[str]:
    """Return list of missing required fields."""
    return [f for f in step.handoff.required if f not in handoff]
```

So `status: in_progress` and `status: complete` are accepted identically.
The step loop advances, the run walks to the end of its step list, and the
`finally` block emits `agent/session.completed` for work that was never done.

This is the root cause behind the tail-death we have been chasing on Lane A:
sessions that report success while their structured steps died mid-way.

### Observed in production on this host

Three `pr-feedback` runs handed off a status meaning "I found nothing to do" and
were recorded as completed:

| Session | `handoff-address.yaml` | Recorded status |
| --- | --- | --- |
| `wf-pr-feedback-eng-team-adhoc-6fa1e5cd` | `status: no_reviewer_feedback_found` | `completed` |
| `wf-pr-feedback-eng-team-adhoc-624065c5` | `status: no_reviewer_feedback_found` | `completed` |
| `wf-pr-feedback-eng-team-adhoc-64b3afa1` | `status: no_reviewer_feedback_found` | `completed` |

The clearest case is the `issue-lifecycle` run `wf-issue-lifecycle-eng-team-adhoc-6c040187`.
Its `implement` step handed off:

```yaml
status: in_progress
phase: implement_in_progress
pushed: false
pr_url: ""
remaining_before_complete:
  - commit Phase 4 groups D/E/F (files landed, not yet committed)
  - full unit suite green (running)
  - integration gate for the touched surfaces
  - house review gate -> findings fixed -> LANDABLE verdict pinned to head SHA
  - push branch
```

The agent stated in the handoff that the step was unfinished and enumerated what
remained.
The engine advanced to `pr`, then `qa`, and `state.json` for that run records
`"status": "completed"` with `"error": ""`.

The agent told the truth.
The engine could not read it.

### Compounding defect: `pr-feedback` never receives its task

`agents/eng-team/workflows/pr-feedback.yaml:10-17` hardcodes its premise and
never interpolates `${{input.task}}`:

```yaml
    prompt: |
      A reviewer requested changes on the PR.
      Follow the Feedback Phase instructions in your role prompt.
```

A dispatched task never reaches the step.
The agent is told only that "a reviewer requested changes", finds no such review,
writes `status: no_reviewer_feedback_found`, and the run is recorded as a success.
That is how the three sessions above happened.

## Goals

- A step whose handoff reports unfinished or failed work must fail the step and
  surface a real terminal status, never `agent/session.completed`.
- The engine must be able to express "what counts as success" per step, in the
  workflow YAML, without an LLM in the decision path.
- The gate must fail closed: an unsatisfied condition, an unparseable condition,
  or a missing value all fail the step.
- Existing workflows keep working unchanged until they opt in.
- A `pr-feedback` run must receive the dispatched task.

## Non-Goals

- Inferring completion semantically from the agent's prose.
  The gate is a declared condition over declared handoff fields, evaluated by
  the existing safe parser.
- Retrying or repairing a step that reports unfinished work.
  See Decision 2.
- Adopting the gate across every shipped pack in this PR.
  Only the eng-team steps with an observed false completion are gated here.
- Changing what a step failure does downstream.
  A failed step already fails the run and emits `agent/session.failed`; this
  change only makes more steps correctly reach that path.

## Root Cause

The engine has a contract mechanism (`handoff.required`) and no predicate
mechanism.
The contract answers "did the agent respond in the required shape", which the
engine then treats as the answer to "did the agent do the work".
Those are different questions, and nothing in the engine ever asked the second
one.

Two smaller seams sit next to it and produce the same class of false completion:

1. **The handoff-retry drain is unchecked.**
   `orchestrator.py:875-877` calls `_drain_response` and discards its result:

   ```python
   await client.query(fix_prompt)
   await _drain_response(client, session_name, run_key, model=current_model)
   ```

   The primary drain at `:852-861` checks `final_text is None` and fails the
   step. The retry drain does not, so a turn that dies while fixing a handoff is
   silently ignored.

2. **`StepDef.timeout` is parsed and never read.**
   `schema.py:37,97` parse it; nothing on `main` consumes it.
   `docs/WORKFLOW_ENGINE.md:97-99` states that it "is the declared deadline
   carried into the registry for the reconciler's dead-man check", which is not
   true: the registry receives the **run-level** timeout
   (`orchestrator.py:207`), never `step.timeout`.

## Proposed Solution

### 1. Add `handoff.success_when`

An optional condition on the handoff block, evaluated after the required-field
check, using the existing safe parser (`variables.py` `evaluate_condition`).

```yaml
  - name: implement
    agent: engineer
    prompt: |
      Follow the Implement Phase instructions in your role prompt.
    handoff:
      required: [status]
      success_when: "status == 'complete'"
```

Schema (`bobi/workflow/schema.py`):

```python
@dataclass
class HandoffContract:
    required: list[str] = field(default_factory=list)
    optional: list[str] = field(default_factory=list)
    success_when: str = ""
```

Orchestrator: after `outputs` is captured and before `agent/step.completed`, an
unsatisfied condition fails the step through the existing failure path
(`_emit_step_failed` plus the honest terminal `finally`).

The `agent/step.failed` payload carries the condition and the actual handoff
values, so the operator sees why:

```
Step implement handoff did not satisfy success_when "status == 'complete'"
(handoff: status='in_progress')
```

#### Decision 1: the condition sees only this step's handoff

The condition is evaluated in a **fresh** `VariableContext` seeded with this
step's outputs, not the run-wide flat scope.

This is load-bearing, not stylistic. The run-wide flat scope accumulates every
prior step's outputs, so a step that omits a field inherits an earlier step's
value for it. Measured against the real parser:

```
--- stale-value leak: step omits 'status', an earlier step set it ---
  True   <- this step's handoff has no 'status' at all
```

A gate that reads the run-wide scope would pass a step that wrote no status at
all, which is the exact false-pass this ticket exists to close.
The leak is orchestrator-side scoping and reproduces identically under the
rewritten parser in #844, so an isolated context is required either way.

Fields are visible both bare (`status == 'complete'`) and scoped
(`${{handoff.status}}`), matching how route steps already read step outputs.
Visibility is the step's **declared** fields (`required + optional`), the same
set that becomes the step's output scope, so the engine keeps one rule for what
a step's interface is.

#### Decision 2: a failed gate is not retried

`MAX_HANDOFF_RETRIES` exists to repair a **malformed file** by re-prompting for
missing fields.
A failed `success_when` is not a malformed file. It is an accurate report of
unfinished work.

Re-prompting there would ask an agent that correctly wrote `status: in_progress`
to write `status: complete` instead, which trains the exact dishonesty this
ticket is trying to remove.
The gate fails the step immediately.

#### Decision 3: fail closed

- Condition unsatisfied: step fails.
- Condition references a field not in the step's declared handoff: it resolves
  to nothing, the condition is unsatisfied, the step fails, and the error names
  the condition and the visible fields.
- Condition unparseable or raising: step fails.

#### Decision 4: opt-in

No `success_when` means today's behavior exactly. Every existing workflow and
every pack in the wild is unaffected until it opts in.

#### Decision 5: reject unknown keys inside the `handoff:` block

A fail-closed gate that a typo can silently delete is not fail-closed.
`load_workflow` today does `s.get(...)` per key and ignores everything else, and
there is no repo-wide workflow schema lint, so `success_if:` or
`success-when:` would parse cleanly and ship a step with no gate.

The loader will reject unknown keys inside `handoff:` (whitelist: `required`,
`optional`, `success_when`) and reject a step-level `success_when:` placed
outside the `handoff:` block.
This is a deliberate strictness increase, scoped to the handoff block only, and
it surfaces through the existing pack validation (`bobi/setup/actions.py:270-282`).

### 2. Adopt the gate where a false completion was observed

| Workflow | Step | `success_when` |
| --- | --- | --- |
| `issue-lifecycle.yaml` | `implement` | `status == 'complete'` |
| `issue-lifecycle.yaml` | `qa` | `qa_status in ['pass', 'not_applicable']` |
| `pr-feedback.yaml` | `address` | `status == 'complete'` |

`qa` needs the allow-list because its own prompt instructs `not_applicable` as a
legitimate pass.

Deliberately **not** gated in this PR:

- `stall-recovery.recover`: its prompt explicitly offers two acceptable outcomes
  ("either resume or report the issue"), so a gate needs a vocabulary decision
  first, and a stall-recovery failure has no recovery workflow behind it.
- `build-failure.fix`, `merge-conflict.resolve`, `pr-closed.close-issue`: no
  observed false completion; gating them is a follow-up.
- `personal-assistant` and `dogfood-content-review` packs: not this team's lane,
  and each has sentinel values (`awaiting_confirmation`, `not_applicable`) that
  need their own decision.

Gating `status` requires the role prompt to state the allowed values.
`agents/eng-team/roles/engineer/ROLE.md` currently tells the engineer to write
`phase: implement_complete` and never describes `status`, which is why real
handoffs carry free text like `status: build in progress - Phase 1 complete`.
The ROLE.md phase instructions gain the `status` vocabulary in the same PR.

### 3. Fix `pr-feedback` task interpolation

Interpolate `${{input.task}}` so the dispatched task reaches the step, instead
of the hardcoded "A reviewer requested changes on the PR" premise.

### 4. Honor the retry drain error

Give `orchestrator.py:875-877` the same treatment `:856` already has: a turn that
dies during a handoff-fix retry fails the step instead of being discarded.

### 5. `StepDef.timeout`: correct the documentation, do not add a second enforcer

The ticket says "enforce or delete". Neither is right in this PR, because #847
(issue #845) **already makes the field live** on its branch, as the wall-clock
bound on turn-budget resumes:

```python
and time.time() - step_start < step.timeout
```

- Deleting it would break #847.
- Adding a separate hard per-step deadline here would be a second enforcement
  path for one concept, and would newly fail steps whose declared budgets are
  optimistic (`setup: 300`, `plan_review: 600`) - a regression on the very lane
  this work is stabilizing.

What remains genuinely broken and in scope is the **false claim** in
`docs/WORKFLOW_ENGINE.md:97-99`. That line is corrected to describe what
`timeout` actually is; #847 owns making it load-bearing.

### 6. Documentation

Same PR, per the repo's verification rule:

- `docs/WORKFLOW_ENGINE.md`: the handoff contract section gains `success_when`
  and the step-completion semantics; the `timeout` claim is corrected; the
  `agent/step.failed` row covers the new cause.
- `skills/create-agent.md:193-226, 443-450`: the canonical pack-format
  reference gains `success_when`.
- `agents/eng-team/roles/engineer/ROLE.md`: the `status` vocabulary.

## Dependency And Landing Order

**#846 must land on top of #844 (Lane A).** This is a hard dependency, not a
preference.

#844 rewrites `evaluate_condition` so operands resolve inside the parser instead
of by textual substitution. Two consequences matter here:

1. **The allow-list idiom is broken on `main`.** `qa` needs
   `qa_status in ['pass', 'not_applicable']`, and on `main` `in` against a list
   literal stringifies the list and does a substring test. Measured:

   | `qa_status` | `main` | #844 |
   | --- | --- | --- |
   | `pass` | True | True |
   | `not_applicable` | True | True |
   | `fail` | False | False |
   | `p` | **True** | False |
   | `ass` | **True** | False |

   Shipping the `qa` gate on `main`'s parser would ship a gate that admits
   fragments.

2. **Handoff values are untrusted text.** On `main` a value is substituted into
   the expression before parsing, so a value containing ` in `, ` and `, or a
   quote re-tokenizes the condition. (Probing plausible free-text statuses
   against a `==` gate did not produce a wrong pass, so this is a robustness
   argument, not a demonstrated false pass - but a fail-closed gate should not
   depend on that luck.)

#844 also adds the per-visit stale-handoff unlink that `success_when` needs to
be correct on a route-loop back-edge, a hazard already recorded at
`plans/2026-07-22-review-remediation-findings.md:193`.

**#847 (issue #845) overlaps textually.** It refactors `_drain_response` to
return a `DrainResult` NamedTuple and rewrites the region where fix 4 lands, and
it owns `step.timeout`. The changes compose; whichever lands second rebases.

## Verification Plan

Failing-test-first: each test below is written to fail on the current engine and
pass after the change.

### The headline reproduction

`tests/integration/test_workflow_orchestrator.py`, stub brain, real
`run_workflow`, no mocks - the house standard for proving the real flow.

A run whose step handoff reports `status: in_progress` under
`success_when: status == 'complete'` must leave `state.json` with
`"status": "failed"` and a real error, and must not emit
`agent/session.completed`.
This reproduces run `6c040187` directly.

The stub brain has no way to write a handoff file today, and pre-writing one is
fragile across #844's per-visit unlink. A test-only `__stub__:handoff:` directive
is added to `bobi/brain/stub.py` alongside the existing verbs so stub-driven
workflow tests can exercise the handoff path at all. This is new test surface,
gated behind `BOBI_STUB_BRAIN` like the rest of the stub.

### Unit tests (`tests/test_orchestrator.py`)

- An unfinished step fails the run, emits `agent/step.failed` naming the
  condition and the actual value, emits `agent/session.failed`, does **not**
  emit `agent/session.completed`, and never starts the following step.
- A satisfied gate completes as before.
- A failed gate is **not** retried: the agent receives no handoff-fix prompt
  (Decision 2).
- The gate sees only this step's handoff: step A writes `status: complete`,
  step B omits `status` and carries the gate, and B fails (Decision 1 - this
  currently passes, proven above).
- A step with no `success_when` behaves exactly as today (Decision 4).
- An unparseable condition fails the step (Decision 3).
- A turn that dies during a handoff-fix retry fails the step (fix 4).

### Schema tests

- `success_when` parses onto `HandoffContract`.
- An unknown key inside `handoff:` is rejected.
- A step-level `success_when:` outside `handoff:` is rejected (Decision 5).

### Workflow YAML tests (`tests/test_eng_team_role_constraints.py`)

- `pr-feedback.yaml`'s `address` prompt interpolates `${{input.task}}`.
- The three gated steps carry their `success_when`.

### Suites

`pytest tests/ --ignore=tests/integration/ --ignore=tests/e2e/ --timeout=30 -q`
plus `pytest tests/integration/ -m "not claude and not docker"`, both green
before the PR is marked ready.

A real-Claude leg is not required: this change is brain-agnostic. It is a
deterministic predicate over a YAML file the engine reads itself, with no LLM in
the decision path, which the repo's own judgement rule puts on the stub side.

## Risks And Mitigations

| Risk | Mitigation |
| --- | --- |
| Runs that used to report success now report failure. | That is the point, and it is the honest signal. Scoped to three steps with observed false completions; everything else is opt-in. |
| An agent learns to write `status: complete` to get past the gate. | The gate is not retried (Decision 2), so there is no in-run feedback loop teaching it. Longer term this is a role-prompt and review-gate concern, not an engine one. |
| A gate on `qa` turns a real QA failure into a run failure. | Intended. `qa_status: fail` currently completes silently. |
| Free-text `status` values fail the new gate. | The ROLE.md vocabulary change lands in the same PR. |
| Handoff-block key whitelist rejects a pack in the wild. | Only `required` and `optional` exist today; the whitelist is scoped to the handoff block and surfaces through existing pack validation. |
| Conflicts with #844 / #847. | Declared above; #846 rebases onto #844. |

## Open Questions For The Approval Gate

1. **`stall-recovery.recover`** is left ungated because its prompt offers two
   valid outcomes. Gate it with an allow-list, or leave it?
2. **`qa_status: fail`** failing the run is a deliberate behavior change. Confirm.
3. **Landing order**: confirm #846 waits for #844 rather than duplicating the
   parser fixes.
