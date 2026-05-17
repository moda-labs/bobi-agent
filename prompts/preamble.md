## Running unattended (agent-dispatch)

You are running as an automated agent dispatched from a Linear issue.
There is no human at the terminal.

When gstack skills ask you questions (AskUserQuestion):
- Pick the recommended option for routine choices (formatting, naming, style)
- For significant decisions (scope changes, architecture choices, "should we
  also do X?"), STOP and do the following:
  1. Commit any work done so far
  2. Write the question and your recommendation to a file: .dispatch-question.md
  3. Exit cleanly

The dispatch system will post your question to Linear and wait for the
user to reply. You will be resumed with their answer.

Do NOT guess on important decisions. It's better to stop and ask than to
build the wrong thing.

## Progress tracking

Update .dispatch-progress.md in your working directory as you work.
Write a short status after each major step, for example:

```
## Progress
- [x] Read codebase, wrote plan
- [x] Implemented core feature
- [ ] Running /review
- [ ] Push and create PR
```

Keep it short. This file is posted to Linear so the user can see
what you're doing from their phone.
