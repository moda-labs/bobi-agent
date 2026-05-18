# AGD-7: Add timestamp comment to README

## Classification

**Update** — adding new content (a UTC timestamp comment) to the bottom of README.md.

## Problem & Solution

**Problem:** Automated test issue — verifies the agent dispatch loop can make a simple file change, push, and create a PR.

**Solution:** Append an HTML comment containing the current UTC timestamp to the bottom of README.md. HTML comments are invisible when rendered, so this doesn't affect the visible documentation.

**Out of scope:** Changing any other files, updating documentation content, modifying project functionality.

## Scope Guards

- No billing/payment primitives
- No multi-screen user flows
- No schema changes

None apply.

## Size Verdict

**Small** — single file, single line addition, one domain.

## Technical Approach

### Files changed

| File | Change |
|------|--------|
| `README.md` | Append an HTML comment with UTC timestamp at the bottom |

### Design decisions

- Use an HTML comment (`<!-- ... -->`) so the timestamp is invisible in rendered markdown
- Use ISO 8601 format for the timestamp (e.g., `2026-05-17T12:00:00Z`)
- Append to the very end of the file with a trailing newline

### Alternatives considered

1. **Visible markdown text** — Rejected: pollutes rendered README for no user value
2. **YAML frontmatter** — Rejected: overcomplicates a simple addition

## Verification Plan

### Level 1: Unit Tests

No unit tests needed — this is a static file edit with no logic.

### Level 2: Integration Tests

- Verify README.md ends with an HTML comment containing a valid UTC timestamp
- Verify the rest of README.md content is unchanged

### Level 3: Manual QA

1. Open the PR diff and confirm only README.md is changed
2. Confirm the last line is an HTML comment with a UTC timestamp in ISO 8601 format
3. View README.md rendered on GitHub — confirm the timestamp comment is not visible

## Implementation Plan

1. Read current README.md (trivial)
2. Append `<!-- Timestamp: YYYY-MM-DDTHH:MM:SSZ -->` to the end of the file (trivial)
3. Commit and push (trivial)

Estimated complexity: **trivial**
