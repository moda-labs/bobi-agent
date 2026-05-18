# AGD-4: Add timestamp comment to README

## Classification

**Type:** Update
**Size:** Small — single file, one-line change

## Problem & Solution

**Problem:** The dispatch system needs a trivial end-to-end test to verify the scan → dispatch → implement → PR lifecycle works. This issue exercises the full loop with a minimal, low-risk change.

**Solution:** Append an HTML comment containing the current UTC timestamp to the bottom of `README.md`.

**Out of scope:** Any functional changes, formatting changes, or content edits to the existing README.

## Scope Guards

- No billing/payment primitives
- No multi-screen user flows
- No schema changes

## Technical Approach

**File:** `README.md`

Append a single HTML comment at the end of the file:

```markdown
<!-- agentd timestamp: 2026-05-17T00:00:00Z -->
```

The timestamp will be generated at implementation time using UTC (`date -u +%Y-%m-%dT%H:%M:%SZ`).

HTML comments are invisible when rendered, so this change has zero impact on the displayed README.

**Alternatives considered:**
- Visible markdown text: rejected — pollutes the rendered README for a test artifact
- Separate file: rejected — the issue specifically says README.md

## Verification Plan

### Level 1: Unit Tests

N/A — this is a static file edit with no code logic to unit test.

### Level 2: Integration Tests

N/A — no code paths affected.

### Level 3: Manual QA

1. Open the PR diff and verify a single HTML comment was appended to `README.md`
2. Confirm the comment contains a valid UTC timestamp in ISO 8601 format
3. Confirm no other lines in `README.md` were modified
4. View the rendered README on GitHub and confirm the comment is not visible

## Implementation Plan

1. Generate current UTC timestamp
2. Append `<!-- agentd timestamp: {UTC_TIMESTAMP} -->` as the last line of `README.md`
3. Commit and push
4. Create PR

**Complexity:** Trivial
