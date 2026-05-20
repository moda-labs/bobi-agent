# /feedback — Address review comments

You are addressing feedback from a human reviewer. Fix what they asked for,
review your fixes, push.

Refer to `domains/source-control` for commit/push conventions,
`domains/code-review` for mandatory review of fixes.

## Steps

### 1. Understand the feedback

Read the human's reply. Categorize:
- **Fix**: something wrong → change it
- **Question**: needs clarification → ask the manager (just explain what
  you need, the manager will see you're idle and check on you)
- **Suggestion**: nice-to-have → use judgment

### 2. For bugs: /investigate

If the feedback points to a bug, follow the bug workflow in
`domains/code-review` — invoke `/investigate` for root cause.

### 3. Make changes

Spawn a sub-agent with the feedback items and relevant source files.
It makes fixes and commits them.

### 4. Review fixes with /review

Follow mandatory review in `domains/code-review`. Give `/review` just
the diff since the last push. Fix anything it finds.

### 5. Run tests

Run the project's test command.

### 6. Push

```bash
git push
```

## Rules

- Only change what was requested. Don't refactor unrelated code.
- If feedback contradicts the spec, follow the feedback (human overrides spec).
- `/review` on fixes is mandatory per `domains/code-review`.
- Commit format per `domains/source-control`: `[ISSUE-ID] type: description`
