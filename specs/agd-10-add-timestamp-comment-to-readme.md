# AGD-10: Add timestamp comment to README

## Classification

**Update** — adding a new element to an existing file.

## Problem & Solution

**Problem:** The dispatch system needs a simple end-to-end test to verify the full agent lifecycle (scan → spec → implement → PR). This issue exercises that loop with a trivially scoped change.

**Solution:** Append an HTML comment containing the current UTC timestamp to the bottom of `README.md`.

**Out of scope:** Any functional changes to the codebase. This is purely a test artifact.

## Scope Guards

- No billing/payment primitives
- No multi-screen user flows
- No schema changes

## Size Verdict

**Small** — single line added to a single file.

## Technical Approach

### Files changed

- `README.md` — append an HTML comment at the end: `<!-- agentd timestamp: YYYY-MM-DDTHH:MM:SSZ -->`

### Design decisions

- Use an HTML comment (`<!-- -->`) so the timestamp is invisible when rendered but visible in source
- Use ISO 8601 UTC format for unambiguous timestamps
- Capture the timestamp at implementation time (not spec time)

## Verification Plan

### Level 1: Unit Tests

N/A — no testable logic. This is a static file edit.

### Level 2: Integration Tests

N/A — no integration surface.

### Level 3: Manual QA (human gate)

1. Open the PR diff
2. Confirm `README.md` has exactly one new line at the bottom
3. Confirm the new line is an HTML comment matching `<!-- agentd timestamp: YYYY-MM-DDTHH:MM:SSZ -->`
4. Confirm no other lines were changed
5. View README.md rendered on GitHub — confirm the comment is not visible

## Implementation Plan

1. Read current `README.md` (trivial)
2. Append `<!-- agentd timestamp: {UTC_NOW} -->` with a trailing newline (trivial)
3. Commit and push (trivial)

**Estimated complexity:** Trivial
