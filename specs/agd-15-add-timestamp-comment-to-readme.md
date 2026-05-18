# AGD-15: Add Timestamp Comment to README

## Classification

**Type:** Update (trivial)
**Size:** Small — single file, single line change

## Problem & Solution

**Problem:** The dispatch system needs a simple end-to-end test to verify the full agent lifecycle (scan → spec → implement → PR). This test issue exercises that loop with a minimal, zero-risk change.

**Solution:** Append an HTML comment containing the current UTC timestamp to the bottom of `README.md`.

**Out of scope:** Any functional changes to the codebase. This is purely a test artifact.

## Scope Guards

- No billing/payment primitives
- No multi-screen user flows
- No schema changes

None apply.

## Technical Approach

**File:** `README.md`

Append a single HTML comment line at the end of the file:

```markdown
<!-- agentd timestamp: 2026-05-17T00:00:00Z -->
```

The timestamp will be the current UTC time at the moment of implementation.

**Trade-offs:** None. This is a one-line append with no side effects.

## Verification Plan

### Level 1: Unit Tests

N/A — no testable logic. This is a static file edit.

### Level 2: Integration Tests

N/A — no integration points.

### Level 3: Manual QA (human gate)

1. Open `README.md` in the PR diff
2. Verify the last line is an HTML comment containing a UTC timestamp
3. Verify the comment does not render visibly in GitHub's README preview
4. Verify no other lines in README.md were changed

## Implementation Plan

1. Read `README.md` (trivial)
2. Append HTML comment with UTC timestamp (trivial)
3. Commit and push (trivial)

**Estimated complexity:** Trivial
