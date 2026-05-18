# AGD-12: Add timestamp comment to README

## Classification

**Update** — Small, single-file change.

## Problem & Solution

**Problem:** The README.md needs a UTC timestamp comment appended to its end, serving as an automated test to verify the agent dispatch pipeline works end-to-end.

**Solution:** Append an HTML comment containing the current UTC timestamp (ISO 8601) to the bottom of README.md.

**Out of scope:** Changing any other file, modifying README content, adding visible text.

## Scope Guards

- No billing/payment primitives
- No multi-screen user flows
- No schema changes

## Size Verdict

**Small** — One line appended to one file.

## Technical Approach

### Files changed

- `README.md` — Append an HTML comment with UTC timestamp at the end of the file

### Design

Append a single line in the format:

```
<!-- Timestamp: 2026-05-17T00:00:00Z -->
```

Using an HTML comment keeps the timestamp invisible in rendered markdown. The timestamp is generated at implementation time using UTC.

### Alternatives considered

- Visible text: Rejected — adds noise to the rendered README for a test-only change.
- Separate file: Rejected — the issue explicitly says README.md.

## Verification Plan

### Level 1: Unit Tests

N/A — This is a single-line append to a markdown file. No application logic to unit test.

### Level 2: Integration Tests

N/A — No integration surface. Verification is visual inspection.

### Level 3: Manual QA (human gate)

1. Open the PR diff
2. Confirm the last line of README.md is an HTML comment in the format `<!-- Timestamp: <ISO 8601 UTC> -->`
3. Confirm no other lines in README.md were changed
4. View the rendered README on GitHub and confirm the timestamp is not visible

## Implementation Plan

1. Read current README.md (trivial)
2. Append HTML comment with current UTC timestamp (trivial)
3. Commit and push (trivial)

Estimated complexity: **trivial**
