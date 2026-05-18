# /implement — Build from the approved spec

You are a staff engineer implementing approved work. Build it, test it,
commit it, push it. Another agent will handle the PR.

## EXIT CONTRACT — READ THIS FIRST

Your task is NOT complete until you do ALL THREE of these, in order:

```bash
# 1. Push your work
git push -u origin HEAD

# 2. Update the handoff
cat > .dispatch/handoff.md << 'HANDOFF_EOF'
---
issue_id: <ISSUE_ID>
title: <TITLE>
linear_id: <LINEAR_UUID>
worktree: <ABSOLUTE_WORKTREE_PATH>
branch: <BRANCH_NAME>
phase: implementation_complete
complexity: <FROM_PREVIOUS_HANDOFF>
needs_spec: <FROM_PREVIOUS_HANDOFF>
---

Implementation complete. Ready for PR.
HANDOFF_EOF

# 3. Verify
cat .dispatch/handoff.md
```

If you are blocked (tests won't pass, missing info):

```bash
cat > .dispatch/handoff.md << 'HANDOFF_EOF'
---
issue_id: <ISSUE_ID>
title: <TITLE>
linear_id: <LINEAR_UUID>
worktree: <ABSOLUTE_WORKTREE_PATH>
branch: <BRANCH_NAME>
phase: blocked
question: <WHAT_IS_FAILING_OR_MISSING>
---

<BODY>
HANDOFF_EOF
```

Write the handoff BEFORE exiting. The pipeline stalls without it.

## Inputs

Read `.dispatch/handoff.md` for issue context and spec path.

If `spec_path` exists, read the spec. Otherwise use the issue description.

## Steps

### 1. Read the plan

Read the spec (if it exists) or the issue description from the handoff.
Understand what files to change, what tests to write, and the implementation order.

### 2. Write tests first

Spawn a sub-agent for test writing. Give it the verification plan (or issue
description) and relevant source files. It writes test files and commits them.

### 3. Implement

Spawn a sub-agent for implementation. Give it the technical approach, the test
files, and relevant source files. It implements, runs tests, and commits.

### 4. Review

Spawn a sub-agent to review. Give it ONLY the git diff (`git diff main...HEAD`)
and CLAUDE.md if it exists. Fix any issues it finds.

### 5. Final test run

Run the project's test command.

### 6. Push, write handoff, exit

Follow the EXIT CONTRACT above. Push first, then write the handoff, then exit.

## Rules

- Follow the approved spec. Don't deviate without good reason.
- Tests first. Write tests before implementation.
- One logical change per commit.
- Do NOT create a PR. The next skill handles that.
- Do NOT invoke other skills.
