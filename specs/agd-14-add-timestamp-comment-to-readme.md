# AGD-14: Add timestamp comment to README

## Classification

**Update** — Small, single-file change.

## Problem & Solution

**Problem:** The README needs a UTC timestamp comment appended at the bottom for automated test verification.

**Solution:** Append an HTML comment (`<!-- ... -->`) containing the current UTC timestamp to the end of `README.md`.

**Out of scope:** Modifying any existing README content; adding visible text; recurring timestamp updates.

## Scope Guards

None triggered — no billing, no multi-screen flows, no schema changes.

## Size

**Small** — One file, one line added.

## Technical Approach

- Append an HTML comment to the end of `README.md` in the format: `<!-- Timestamp: YYYY-MM-DDTHH:MM:SSZ -->`
- HTML comments are invisible to rendered Markdown, so this has zero impact on the displayed README.

### Files changed

| File | Change |
|------|--------|
| `README.md` | Append one HTML comment line at EOF |

## Verification Plan

### Level 1: Unit Tests

Not applicable — this is a static content addition with no code logic.

### Level 2: Integration Tests

Not applicable — no API or system interaction.

### Level 3: Manual QA

1. Open `README.md` in the PR diff
2. Confirm a single HTML comment line was appended at the bottom
3. Confirm the comment contains a valid UTC timestamp in ISO 8601 format
4. Confirm no existing content was modified
5. View the rendered README on GitHub and confirm the comment is invisible

## Implementation Plan

1. Append `<!-- Timestamp: <current UTC> -->` to `README.md` (trivial)
2. Commit and push
3. Create PR

Estimated complexity: **trivial**
