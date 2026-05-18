# AGD-9: Review README.md

## Classification

**Update** — documentation out of sync with codebase.

## Problem

The README has not been updated to reflect several significant code changes:
- State transitions moved from agents to the Python engine
- New `linear_state.py` module not listed in project structure
- Stall timeout changed from 5 min to 10 min
- New workflow states (Planning, Implementing) not documented
- Architecture diagram doesn't reflect the state transition step
- Issue lifecycle is missing several states and transitions

## Discrepancies

### 1. Architecture diagram (lines 9-23)
**README says:** 4-step cycle: SCAN → RECONCILE → RE-SPAWN → DISPATCH
**Code says:** 7-step cycle (engine.py docstring): Poll → Reconcile → Stall detection → State transitions → Re-spawn → Check merged PRs → Dispatch

### 2. "Agents post their own status updates" (line 25)
**README says:** "Agents post their own status updates and questions as Linear comments prefixed with 🤖"
**Code says:** Engine owns ALL state transitions and comments via `linear_state.py`. Agents just do work and exit.

### 3. Project structure (lines 134-148)
**README missing:** `linear_state.py` — the module that handles all Linear API state transitions (move_issue, add_comment, has_spec, has_pr, is_pr_merged, has_question).

### 4. Stall timeout
**README says:** "agents with no activity for 5 minutes are killed" (line 132)
**Code says:** `STALL_TIMEOUT_SECONDS = 600` — 10 minutes (engine.py line 31)

### 5. Issue lifecycle (lines 119-131)
**README shows:** Todo → Design Review → In Review → Blocked → Done/Canceled
**Code shows:** Todo → Planning → Design Review → Implementing → In Review → Done. Also Blocked for questions, with auto-transitions between states.

### 6. Board states
`board_setup.py` defines: Planning, Design Review, Implementing, Blocked, In Review.
README lifecycle section doesn't mention Planning or Implementing.

### 7. Auto-close on merge
Engine step 6 checks In Review issues for merged PRs and auto-moves to Done. Not documented.

## Out of scope

- Changing CLAUDE.md (separate concern, different audience)
- Functional code changes
- Slack notification stubs (not implemented, shouldn't be documented)

## Size verdict

**Small** — one file (README.md), documentation only.

## Technical approach

Update README.md sections in this order:
1. Architecture diagram — reflect the 7-step cycle
2. Remove "agents post their own" claim, replace with engine-owned transitions
3. Project structure — add `linear_state.py`
4. Issue lifecycle — add Planning and Implementing states, fix stall timeout
5. Design decisions — update "Agent posts own comments" row

## Verification plan

**Level 1:** N/A (no code changes)
**Level 2:** N/A (no code changes)
**Level 3 (Manual QA):**
1. Read updated README end-to-end
2. Cross-reference each section against the corresponding source file
3. Verify the architecture diagram matches engine.py's actual cycle
4. Verify the issue lifecycle matches the state transitions in engine.py
5. Verify the project structure lists all files in dispatch/

## Implementation plan

1. Update architecture diagram
2. Update agent behavior description
3. Update project structure
4. Update issue lifecycle section
5. Update design decisions table
6. Fix stall timeout mention

Estimated complexity: **trivial**
