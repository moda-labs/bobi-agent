## Running unattended (agentd)

You are an automated agent dispatched from a Linear issue.
There is no human at the terminal. You work alone and exit when done.

### Decision handling

- Routine choices (formatting, naming, style): pick the recommended option
- Significant decisions (scope changes, architecture, "should we also do X?"):
  1. Write the question + your recommendation to `.dispatch-question.md`
  2. Update `.dispatch/state.md` with what happened
  3. Exit cleanly — dispatch will post your question to Linear

### Progress tracking

Update `.dispatch-progress.md` as you work:

```
- [x] Read codebase
- [x] Wrote spec
- [ ] Pushing and creating PR
```

### Rules

- Do ONE phase per spawn, then exit
- Always read `.dispatch/state.md` first if it exists
- Always write `.dispatch/state.md` before exiting
- Always append to `.dispatch/history.md` after each significant action
- Do NOT loop or wait for human responses — exit and let dispatch re-spawn you
- Do NOT move Linear issue states — the dispatch system handles all state transitions
- Do NOT run files in tests/integration/ — they create real Linear issues
- Do NOT create Linear issues or call the Linear API
- Do NOT modify files outside your worktree
- When running tests, use: pytest tests/ --ignore=tests/integration/
