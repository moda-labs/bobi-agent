# /implement — Build from the approved spec

You are a staff engineer implementing approved work. Build it, test it,
review it, push it.

Refer to `practices/source-control-conventions` and `tools/git` for
commit/push conventions, `practices/code-review` for mandatory quality gates.

## Steps

### 1. Read the plan

Read `~/.modastack/handoffs/<ISSUE_ID>.md`. If `spec_url` exists, fetch the spec
from the issue description (`gh issue view <NUMBER> --json body --jq .body`).
Otherwise use the issue description and triage notes.

### 2. For bugs: /investigate first

If the handoff says this is a bug, follow the bug workflow in
`practices/code-review` — invoke `/investigate` for root cause analysis
before writing any fix.

### 3. Write tests first

Spawn a sub-agent for test writing. Give it the verification plan
and relevant source files. It writes test files and commits them.

### 4. Build with /build

Invoke `/build` for the implementation. Give it the spec, the test
files, and relevant source files.

### 5. Review with /review

Follow the mandatory review process in `practices/code-review`.
Fix everything `/review` finds before continuing.

### 6. QA (if applicable)

If the project has a web frontend, invoke `/qa` per `practices/code-review`.

### 7. Final test run

Run the project's test command.

### 8. Push

Follow push conventions in `tools/git`.

```bash
git push -u origin HEAD
```

### 9. Update handoff

After pushing, update `~/.modastack/handoffs/<ISSUE_ID>.md`:
- Set `phase: implement_complete` (use this exact string)
- Summarize what was implemented

## Rules

- Follow the approved spec. Don't deviate without good reason.
- Tests first. Write tests before implementation.
- Commit conventions: `[ISSUE-ID] type: description`
- `/review` is mandatory. Do not skip it.
- Do NOT create a PR. The `/prepare-pr` phase handles that.
- When a PR *is* created (in `/prepare-pr`), it **must target `main`** — never
  a feature branch — unless you were explicitly instructed to use a different
  base. See `practices/source-control-conventions`.
- Do NOT run `/land-and-deploy` or merge anything.

