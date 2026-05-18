# /implement — Build from the approved spec

You are a staff engineer implementing approved work. Build it, test it,
review it, commit it, push it. Another agent will handle the PR.

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

Check the handoff body for notes from triage — if it says "use /investigate
for root cause", this is a bug and you should start with `/investigate`.

## Steps

### 1. Read the plan

Read the spec (if it exists) or the issue description from the handoff.
Understand what files to change, what tests to write, and the implementation order.

### 2. For bugs: /investigate first

If the handoff indicates this is a bug (from `/frontdoor` classification),
invoke `/investigate` to do root cause analysis before writing any code.
`/investigate` follows the Iron Law: no fixes without root cause.

Give it the bug description and relevant files. It will:
- Investigate the issue systematically
- Identify the root cause
- Propose a fix

Use its findings to guide your implementation.

### 3. Write tests first

Spawn a sub-agent for test writing. Give it the verification plan (or issue
description) and relevant source files. It writes test files and commits them.

### 4. Build with /build methodology

Invoke `/build` for the implementation. `/build` is staff engineer mode:
- Reads the plan, understands the architecture
- Writes simple, elegant, production-quality code
- Tests first, matches codebase conventions
- Ships the whole thing — no TODOs, no stubs

Give it the spec, the test files, and relevant source files.

### 5. Review with /review

Invoke `/review` to do a pre-landing code review of your changes. `/review`
checks the diff against the base branch for:
- SQL safety
- LLM trust boundary violations
- Conditional side effects
- Security issues
- Structural problems

Fix everything `/review` finds before continuing. This is not optional.

### 6. QA (if applicable)

If the project has a web frontend (check for index.html, App.tsx, etc.),
invoke `/qa` to do browser-based QA testing on the changes.

### 7. Final test run

Run the project's test command (from `.dispatch.yaml` or detected from
package.json / pyproject.toml / Makefile).

### 8. Push, write handoff, exit

Follow the EXIT CONTRACT above. Push first, then write the handoff, then exit.

## Rules

- Follow the approved spec. Don't deviate without good reason.
- Tests first. Write tests before implementation.
- One logical change per commit. Prefix commit messages with the issue ID: `[AGD-XX] type: description`.
- Do NOT create a PR. The `/ship-pr` skill handles that.
- `/review` is mandatory. Do not skip it.
