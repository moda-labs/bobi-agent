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

### Compounding defect: the `pr-feedback` step prompt contradicts the run task

`agents/eng-team/workflows/pr-feedback.yaml:10-17` hardcodes its premise and
never interpolates `${{input.task}}`:

```yaml
    prompt: |
      A reviewer requested changes on the PR.
      Follow the Feedback Phase instructions in your role prompt.
```

The dispatched task does reach the *session* - on a fresh run the engine connects
with `task` as the initial prompt (`orchestrator.py:576`) - but it never reaches
the *step's* prompt.
The agent therefore gets a contradiction: turn 1 says "FINISH Lane A PR #844 to a
provable LANDABLE state", and the step prompt says "A reviewer requested changes
on the PR".

It resolved the contradiction by following the step prompt, went looking for a
review, and found none. From the session log of `...-6fa1e5cd`:

> The step's premise was false again. "A reviewer requested changes" - there is
> no review. Verified across six surfaces: `reviews` `[]`, 0 inline comments, 0
> conversation comments, no requested reviewers, no timeline review events,
> `reviewDecision: REVIEW_REQUIRED`.

It then wrote `status: no_reviewer_feedback_found`, and the engine recorded the
run as a success.
That is how the three sessions above happened.

## Goals

- A step whose handoff reports unfinished or failed work must fail the step and
  surface a real terminal status, never `agent/session.completed`.
- The engine must be able to express "what counts as success" per step, in the
  workflow YAML, without an LLM in the decision path.
- The gate must fail closed: an unsatisfied condition, an unparseable condition,
  or a missing value all fail the step.
- Existing workflows keep working unchanged until they opt in.
- A `pr-feedback` step's prompt must carry the dispatched task, so the agent is
  not handed a premise that contradicts its own task.

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
      required: [status, phase]
      success_when: "phase == 'implement_complete'"
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

#### The failure an operator reads

An operator hitting this at 2am has exactly two branches, and they need opposite
responses:

- the agent honestly reported unfinished work -> inspect the work, re-dispatch
- the gate references a field nobody writes -> fix the YAML or the role prompt

One message shape serves both. It names the condition, every key actually present
in the handoff file (marked declared or undeclared), which referenced names
resolved, and the path to the file:

```
Step implement handoff did not satisfy success_when "phase == 'implement_complete'"
  resolved:   phase='implement_in_progress'
  unresolved: (none)
  handoff:    phase, status, issue_id, branch, pushed, pr_url,
              remaining_before_complete  [undeclared: issue_id, branch, pushed,
              pr_url, remaining_before_complete]
  file:       <session>/handoff-implement.yaml
```

`unresolved:` is what separates the two branches: a name listed there is a
misconfigured gate, not a dishonest agent.

Printing the present keys matters. In run `6c040187` the `implement` step
declared only `status`, so a message limited to declared fields would hide
`remaining_before_complete` - the one field that told the operator what was left
to do. Values are elided past a fixed length, because handoff values are free
text and this string lands in `state.json`, the event payload, and Slack.

#### `agent/step.failed` wire shape

This is an API surface change, so the payload is specified rather than described.
`_emit_step_failed` gains structured fields alongside the existing `error` and
`text` strings, so programmatic consumers do not parse English:

```json
{
  "run_key": "adhoc-6c040187",
  "workflow": "issue-lifecycle",
  "step": "implement",
  "error": "handoff did not satisfy success_when \"phase == 'implement_complete'\"",
  "text": "Step implement failed: handoff did not satisfy success_when ...",
  "failure_kind": "handoff_gate",
  "success_when": "phase == 'implement_complete'",
  "handoff_resolved": {"phase": "implement_in_progress"},
  "handoff_unresolved": [],
  "handoff_keys": ["phase", "status", "issue_id", "branch", "pushed",
                   "pr_url", "remaining_before_complete"]
}
```

Field semantics: `failure_kind` is a stable vocabulary (`handoff_gate`,
`handoff_missing_fields`, `turn_failed`, `step_exhausted`) so a consumer can
branch without string matching; `handoff_resolved` maps each name the condition
referenced to the value it saw; `handoff_unresolved` lists referenced names that
resolved to nothing; `handoff_keys` is every key present in the file. Existing
consumers reading `error`/`text` are unaffected.

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

Every state a gated step can land in, and what it does:

| State | Outcome |
| --- | --- |
| Condition satisfied | `agent/step.completed`, as today |
| Condition unsatisfied | Step fails, message shape above |
| Condition unparseable or raising | Step fails, `failure_kind: handoff_gate` |
| Condition references an undeclared field | **Rejected at load time** (Decision 5) |
| `success_when` with no declared fields | **Rejected at load time** (Decision 5) |
| Agent wrote no handoff file at all | Existing required-field retry path runs first and fails there; the gate is never reached |
| No `success_when` | Today's behavior exactly (Decision 4) |

**Known limitation this does not close.** A prompt step with no `handoff:` block
at all has `required == []`, so `_validate_handoff` returns `[]` and the step
passes unconditionally - there is nothing to gate on. That describes
`adhoc.yaml`, the most-used workflow in every pack. Opt-in means `adhoc` runs
keep completing regardless of what the agent did.

Closing that needs a different mechanism (a default contract, or a terminal
self-report every step must make), which is a larger design question than this
ticket. Naming it so the gate is not mistaken for whole-engine coverage.

Moving the undeclared-field case to load time is what keeps the runtime message
shape single: at runtime a name can only fail to resolve because the agent
omitted an *optional* declared field, which `unresolved:` reports.

#### Decision 4: opt-in

No `success_when` means today's behavior exactly. Every existing workflow and
every pack in the wild is unaffected until it opts in.

#### Decision 5: catch a broken gate at load time, not in production

A fail-closed gate that a typo can silently delete is not fail-closed.
`load_workflow` today does `s.get(...)` per key and ignores everything else, and
there is no repo-wide workflow schema lint. Verified against the real loader:

```
loaded OK, no error raised
  typo success_if          : silently dropped
  step-level success_when  : silently dropped
```

Both mistakes ship a step with no gate at all. Three load-time checks close it:

1. **Unknown keys inside `handoff:` are rejected** (whitelist: `required`,
   `optional`, `success_when`), and a step-level `success_when:` outside the
   block is rejected. Verified safe: across all 14 shipped workflow YAMLs the
   only handoff-block keys in use are `required` and `optional`.
2. **Every bare name in the condition must be a declared field.** This is the
   same reasoning applied to the likelier mistake. A misspelled *key* leaves the
   step ungated; a misspelled *field reference*
   (`success_when: "statuss == 'complete'"`, or gating a field the author forgot
   to add to `required`) leaves the step **permanently unpassable**, and nothing
   catches it until production.
3. **`success_when` with an empty `required` and `optional` is rejected.**
   `_build_step_prompt` (`orchestrator.py:1186`) only appends handoff
   instructions when `required or optional` is non-empty, so such a step tells
   the agent nothing about what to write, has an empty visible field set, and
   can never pass. Two reasonable behaviors compose into a permanently broken
   step; reject it where it is written.

Errors name the near-miss rather than just refusing, because the whitelist is
the first thing an author meets:

```
workflows/issue-lifecycle.yaml: step 'implement': unknown key 'success_if' in
handoff block (did you mean 'success_when'?). Allowed: required, optional,
success_when.
```

These surface through the existing pack validation
(`bobi/setup/actions.py:270-282`) and `bobi agent <name> workflows validate`,
which also gains a line echoing each parsed gate so an author gets confirmation
at authoring time instead of discovering silence weeks later:

```
Gates: implement -> phase == 'implement_complete'
       qa        -> qa_status in ['pass', 'not_applicable']
```

### 2. Adopt the gate where a false completion was observed

**A gate is only as good as the vocabulary the agent was told to write.**
Every gated field is listed below with the values the role prompt actually
declares today, because a gate over an undeclared field ships a step the agent
cannot reliably satisfy.

| Workflow | Step | Gated field | Values ROLE.md declares today | Proposed gate |
| --- | --- | --- | --- | --- |
| `issue-lifecycle.yaml` | `implement` | `phase` | `implement_complete` (ROLE.md:453) | `phase == 'implement_complete'` |
| `issue-lifecycle.yaml` | `qa` | `qa_status` | `pass`, `fail`, `blocked`, `not_applicable` (ROLE.md:507-510) | `qa_status in ['pass', 'not_applicable']` |
| `pr-feedback.yaml` | `address` | `status` | **none - see below** | `status == 'complete'` |

#### Gate `phase`, not `status`, on `implement`

The eng-team pack already has a completion vocabulary and the engine never used
it. ROLE.md instructs `phase: triage_complete` (:402), `spec_complete` (:435),
`implement_complete` (:453), `pr_ready` (:470).

The headline failure run wrote `phase: implement_in_progress`.
That is an agent reporting honestly *against the vocabulary that already exists*.
A gate of `phase == 'implement_complete'` would have caught run `6c040187` with
no prompt change at all.

Gating `status` instead would mean inventing a second completion vocabulary
next to the one already declared, leaving two near-synonym fields in the same
handoff where one is load-bearing and the other is decorative.
So: `implement` declares `required: [status, phase]` and gates `phase`.
`status` stays required as the human-readable summary line and is explicitly not
the gate.

#### `pr-feedback.address` needs a vocabulary before it can be gated

`pr-feedback.yaml:19` requires `[status, resolution_summary]`, but the Feedback
Phase in ROLE.md (:519-537) only ever mentions `resolution_summary` and **never
mentions `status` at all**.

That gap is the direct cause of the invented `status: no_reviewer_feedback_found`
in all three observed runs: the engine demanded a field the role prompt never
described, so the agent made one up.

Gating that step without first giving the Feedback Phase its vocabulary would
ship a gate the role prompt cannot satisfy. The Feedback Phase therefore gains an
explicit `status` vocabulary (`complete`, plus a named value for "no feedback
found" that is deliberately *not* a pass) in the same PR as the gate.

#### `qa_status: blocked` now fails the run

The allow-list admits 2 of the 4 declared values, so `blocked` fails the run as
well as `fail`. `blocked` is not an accident in the role's design - it is a
designed outcome with its own instructions ("error loudly", set `qa_findings`,
comment on the PR). Failing the run on it is defensible, because a QA run that
could not run is not a QA pass, but it is a **third** behavior change and it is
called out here rather than discovered later. See Open Question 2.

Deliberately **not** gated in this PR:

- `stall-recovery.recover`: its prompt explicitly offers two acceptable outcomes
  ("either resume or report the issue"), so a gate needs a vocabulary decision
  first, and a stall-recovery failure has no recovery workflow behind it.
- `build-failure.fix`, `merge-conflict.resolve`, `pr-closed.close-issue`: no
  observed false completion; gating them is a follow-up.
- `personal-assistant` and `dogfood-content-review` packs: not this team's lane,
  and each has sentinel values (`awaiting_confirmation`, `not_applicable`) that
  need their own decision.

**Rule this establishes:** gating a field means (a) declaring it in `required`,
and (b) the role prompt that writes it declaring its allowed values. A test in
`tests/test_eng_team_role_constraints.py` enforces (b) so the two cannot drift.

### 3. Apply the gate to native action steps too

Prompt steps are not the only place completion is decided without looking at the
result. Native action steps are worse: their completion is not structural, it is
**unconditional**.

`orchestrator.py:678-692`:

```python
if step.action:
    result = _execute_native_action(step, ctx, cwd)
    ctx.set_scope(step.name, result)
    for k, v in result.items():
        ctx.set_flat(k, v)
    _emit_lifecycle_event("agent/step.completed", {
        ...
        "text": f"Native step {step.name} completed: {result.get('status', '')}",
    })
    step_idx += 1
```

`_execute_native_action` returns `{"status": "error", "reason": ...}` for an
unknown action name and for any exception (`:1100-1107`), and
`_cleanup_worktree_action` returns `{"status": "error"}` when it cannot resolve
the target repo (`:1086`). Every one of those emits **`agent/step.completed`**,
with the word `error` interpolated into the completion text, and the run walks on
to report success.

`pr-closed.yaml:9` opens with `action: cleanup_worktree`, so this is live.

The fix needs no new concept. A native action's result dict already becomes the
step's outputs via the same `set_scope` + `set_flat` calls a prompt step uses, so
`success_when` applies to it unchanged:

```yaml
  - name: cleanup
    action: cleanup_worktree
    handoff:
      success_when: "status in ['ok', 'skipped']"
```

This is the one place the spec relaxes Decision 5's rule that `success_when`
requires declared fields: a native action has no `required`/`optional` because
the engine, not an agent, produces its keys. For an action step the visible set
is the result dict itself, and the empty-declared-fields rejection does not apply.

Leaving this out would close the bug for one step type and leave it open in
another, which is why it is in scope despite not being in the ticket.

### 4. Fix `pr-feedback` task interpolation

Interpolate `${{input.task}}` into the step prompt so the step states the actual
task rather than asserting the hardcoded "A reviewer requested changes on the PR"
premise, which the run task may contradict.

Note this is not sufficient on its own, and that is the point of pairing it with
the gate: an agent that finds no feedback still has to report *something*, and
without `success_when` that report is still recorded as success.

### 5. Honor the retry drain error

Give `orchestrator.py:875-877` the same treatment `:856` already has: a turn that
dies during a handoff-fix retry fails the step instead of being discarded.

### 6. `StepDef.timeout`: correct the documentation, do not add a second enforcer

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

### 7. Documentation

Same PR, per the repo's verification rule. The bar is that the section **teaches**
the gate, not that it mentions the key. `docs/WORKFLOW_ENGINE.md:247-263` (the
handoff contract) and `skills/create-agent.md:193-226, 443-450` (the canonical
pack-format reference) must between them convey five things:

1. **Evaluation order**: required-field check -> up to `MAX_HANDOFF_RETRIES`
   repair prompts -> `success_when` -> outputs captured -> `agent/step.completed`.
2. **The scoping difference**: a route `if:` reads the run-wide flat scope; a
   `success_when` reads only that step's declared fields. Same syntax, same file,
   invisible difference, so it has to be stated where the key is introduced.
3. **A failed gate is never retried, and why** (Decision 2).
4. **The failure message shape**, so an operator recognises one before meeting it.
5. **Gating a field means declaring it and teaching its vocabulary** in the role
   prompt that writes it.

Also updated:

- `docs/WORKFLOW_ENGINE.md`: the false `timeout` claim (:97-99) is corrected; the
  `agent/step.failed` row in the lifecycle-events table covers the new cause.
- `docs/BUILDING_AGENT_TEAMS.md:379-388`: where `workflows validate` is
  documented, now that it echoes gates.
- `agents/eng-team/roles/engineer/ROLE.md`: the Implement Phase `phase`
  vocabulary made explicit, and the Feedback Phase given a `status` vocabulary
  it currently lacks entirely.

**Version skew (documented, not fixed).** An older engine's loader ignores
unknown handoff keys, so a pack authored with `success_when` and installed on a
pre-#846 bobi loads clean and runs **ungated** - the same silent-deletion failure
Decision 5 exists to prevent, reintroduced through the install path. `agent.yaml`
has no engine-version floor to declare. In-repo packs are unaffected. This is
called out in the docs and left as a follow-up rather than solved here.

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

2. **Handoff values are untrusted text, and on `main` some of them crash the
   evaluator.** `main` substitutes the value into the expression with
   `re.sub`, where the value becomes the *replacement* string, so a backslash
   escape is read as a regex backreference. Measured:

   | `status` value | `main` | #844 |
   | --- | --- | --- |
   | `failed: bad escape \1 in regex` | **raises `re.error`** | `False` |
   | `C:\temp\1` | **raises `re.error`** | `False` |
   | `complete\g<0>` | `False` | `False` |

   `evaluate_condition` returning an exception instead of a bool is a live
   defect for route steps on `main` today, not only for this gate. Decision 3
   catches it and fails the step closed, but the error an operator would read
   is `invalid group reference 1 at position 20`, which says nothing about the
   handoff. #844 removes the class by never substituting values into expression
   text.

   Value-driven re-tokenization is the same class: a value containing ` in ` or
   ` and ` re-parses the condition. Probing plausible free-text statuses against
   a `==` gate did not produce a wrong *pass*, so that half stays a robustness
   argument - but a fail-closed gate should not depend on that luck.

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

- The failure message names the handoff file path and the keys present in it,
  including undeclared ones, so `remaining_before_complete` is visible.
- A gate whose referenced name resolved to nothing reports it under
  `unresolved:`, distinguishing a misconfigured gate from an honest failure.
- **A native action step returning `status: error` fails the run** rather than
  emitting `agent/step.completed` (fix 3). Drive it through the real gap: an
  unknown action name, which `_execute_native_action:1100` turns into
  `{"status": "error"}` today and the loop reports as completed.
- A gated step placed *after* an `await` step still gates correctly on resume:
  `resume_workflow` restores the run-wide flat scope from `variable_scopes`, and
  the isolated evaluation context must not pick values out of it.
- YAML-typed handoff values behave: `true`/`false`, integers, `null`, and list
  values all evaluate without corrupting the condition.
- The gate does **not** fire on route, notify, or await steps, which have no
  handoff.

### Schema tests

- `success_when` parses onto `HandoffContract`.
- An unknown key inside `handoff:` is rejected, and the error names the
  near-miss (`success_if` -> "did you mean `success_when`?").
- A step-level `success_when:` outside `handoff:` is rejected (Decision 5).
- A condition referencing an undeclared field is rejected at load time.
- `success_when` with empty `required` and `optional` is rejected at load time.
- Every shipped workflow YAML still loads: a test that globs
  `agents/*/workflows/*.yaml` and calls `load_workflow` on each, which does not
  exist today and is what makes the new strictness safe to add.

### Workflow YAML tests (`tests/test_eng_team_role_constraints.py`)

- `pr-feedback.yaml`'s `address` prompt interpolates `${{input.task}}`.
- The three gated steps carry their `success_when`.
- **Every gated field's allowed values appear in the ROLE.md phase that writes
  it**, so a gate and its vocabulary cannot drift apart. This is the test that
  would have caught `pr-feedback.address` being gated on a `status` the Feedback
  Phase never describes.

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
| An agent learns to write the passing value to get past the gate. | The gate is not retried (Decision 2), so there is no in-run feedback loop teaching it. Longer term this is a role-prompt and review-gate concern, not an engine one. |
| `qa_status: fail` turns a real QA failure into a run failure. | Intended. It completes silently today. |
| **`qa_status: blocked` also fails the run.** | A third behavior change, enumerated rather than discovered. `blocked` is a designed outcome (ROLE.md:495, 509), but a QA run that could not run is not a QA pass. Open Question 2. |
| Free-text values fail the new gate. | The ROLE.md vocabulary changes land in the same PR, and a test binds each gate to its role-prompt vocabulary. |
| Handoff-block key whitelist rejects a pack in the wild. | Verified: only `required` and `optional` appear across all 14 shipped workflows. Scoped to the handoff block, surfaces through existing pack validation, and a new load-every-workflow test guards it. |
| A pack authored with `success_when` runs ungated on an older engine. | Documented, not fixed - there is no engine-version floor to declare. In-repo packs unaffected. |
| A gated failure leaves the operator with no Slack message. | `issue-lifecycle` notifies only on start and complete, so a failed run skips `notify_complete`. Verified that `agent/session.failed` is in `LIFECYCLE_EVENTS` (`bobi/events/subscriptions.py:64`) and does route, so the signal exists; the verification plan drives one end to end to confirm what actually lands. |
| Conflicts with #844 / #847. | Declared above; #846 rebases onto #844. |

## Adjacent Finding (not fixed here)

`issue-lifecycle.yaml:111` posts `"Completed #<run>: <task> - PR <url> merged"`,
but nothing in that workflow merges the PR, and landing requires a human
approval the workflow never waits for.

That is the same family as this ticket - a workflow announcing an outcome that
did not happen - but it is a one-line message fix in a file this PR already
touches for other reasons. Flagged rather than folded in, so the gate change
stays reviewable. Say the word and it rides along.

## Open Questions For The Approval Gate

1. **Gate `phase` or `status` on `implement`?** The spec now recommends `phase`,
   because ROLE.md already declares `implement_complete` and the observed failure
   wrote `phase: implement_in_progress` - the gate would have fired with no prompt
   change. Gating `status` instead means inventing a second vocabulary beside the
   one that exists. Confirm the recommendation, or say gate `status`.
2. **`qa_status`: confirm `fail` *and* `blocked` both fail the run.** The
   allow-list admits 2 of the 4 declared values. `blocked` is a designed outcome,
   so this is a deliberate call, not an oversight.
3. **`stall-recovery.recover`** is left ungated because its prompt offers two
   valid outcomes. Gate it with an allow-list, or leave it?
4. **Landing order**: confirm #846 waits for #844 rather than duplicating the
   parser fixes.
5. **Scope check**: the review added load-time reference validation, the
   `workflows validate` gate echo, and the structured `agent/step.failed` payload.
   Each is defensible on its own, but they widen the PR. Say if any should be cut
   to a follow-up.

## Review Record

- **Engineering lens** (2026-07-26, run directly): one material finding, folded
  in as fix 3 - native action steps emit `agent/step.completed` unconditionally,
  including when `_execute_native_action` returns `{"status": "error"}`, so the
  first draft would have closed the bug for prompt steps and left it open for
  action steps. `pr-closed.yaml:9` runs one. Also surfaced the `adhoc` limitation
  above and the resume/YAML-typed-value test cases.
- **Scope lens** (2026-07-26, run directly): the three-step adoption, the
  deferrals, and the `timeout` reinterpretation all hold. The strictness increase
  and the `__stub__:handoff:` directive are in scope, because without the first
  the feature can be silently disabled and without the second the house-standard
  integration test cannot be written at all. ROLE.md must ship in the same PR as
  the gate: a gate without its vocabulary is a broken gate, not a smaller one.
  The one thing found missing was the native action gap.
- **Design / authoring-DX lens** (2026-07-26): scored 5/10, 10 findings.
  Accepted and folded in: the `phase`-vs-`status` vocabulary decision, the
  `pr-feedback` Feedback-Phase vocabulary gap, `qa_status: blocked` as an
  unenumerated third behavior change, load-time validation of condition field
  references, rejection of a gate with no declared fields, the unified failure
  message with the handoff path and present keys, the structured event payload,
  the five docs teachings, and the version-skew note. Verified each against the
  source before accepting.
- **Self-review** caught two of my own errors: the overclaimed "the task never
  reaches the step" (corrected against the real session log) and a stale heading
  left behind by that correction.
- **Cross-model second opinion: NOT AVAILABLE on this host.** `codex` is
  installed but not authenticated (401), and `aichat` has no config
  (`OPENROUTER_API_KEY`, `AICHAT_PLATFORM`, `OPENAI_API_KEY` all unset), so the
  house adversarial-review binding could not run. The two codex-backed legs of
  the spec triple review stalled on it and were re-run as direct reviews, which
  means every lens above is a Claude reading its own author's work. That is a
  real gap in the gate, recorded rather than papered over. If an outside model
  matters before implementation, credentials need fixing first.
