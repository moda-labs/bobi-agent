## Lifecycle

You are working on a single Linear issue. Your worktree is your workspace.
Everything you need to know about where you left off is in `.dispatch/state.md`.

### First thing: read your state

If `.dispatch/state.md` exists, read it. It tells you your current phase,
what happened before, and what to do next. Follow those instructions.

If `.dispatch/state.md` does NOT exist, this is a fresh start. Begin at Phase 1.

### Phases

#### Phase 1: Spec

Write an implementation spec following the methodology in this prompt.
When done:

1. Commit the spec to `specs/`
2. Push the branch: `git push -u origin HEAD`
3. Create a draft PR: `gh pr create --draft --title "[SPEC] {title}" --body "Design review for {issue_id}. Review specs/ and reply 'approved' on Linear."`
4. Move the issue to Design Review (see tools section)
5. Post a Linear comment with the PR link
6. Update `.dispatch/state.md` and `.dispatch/history.md`
7. Exit cleanly — you will be re-spawned when the human responds

#### Phase 2: Implement

The spec was approved. Implement it following the /build methodology.
When done:

1. Push the branch: `git push -u origin HEAD`
2. Create the PR: `gh pr create --title "{title}" --body "Fixes {issue_id}\n\n<description>\n\n## Manual QA\n<Level 3 from spec>"`
3. Move the issue to In Review
4. Post a Linear comment with the PR link and summary
5. Update `.dispatch/state.md` and `.dispatch/history.md`
6. Exit cleanly

#### Phase 3: Address Feedback (if re-spawned with feedback context)

Read the feedback from `.dispatch/state.md`. Fix what was requested.
Push to the same branch (PR updates automatically). Update state. Exit.

### Transitions

| Current state | Trigger | Next phase |
|---|---|---|
| (none) | Fresh dispatch | Phase 1: Spec |
| Spec complete | Human replies "approved" | Phase 2: Implement |
| Spec complete | Human gives feedback | Phase 1 again (revise) |
| Implementation complete | PR merged | Done |
| Implementation complete | PR changes requested | Phase 3: Feedback |

### State file format

Always write `.dispatch/state.md` before exiting:

```markdown
# {issue_id}: {title}

Phase: spec | implement | feedback
Status: complete | in_progress
Last action: <what you just did>
Branch: <branch name>

## Context
- <key facts for next spawn>
- <PR URL if created>
- <approval comment or feedback received>

## Next
- <what the next spawn should do>
```

### History file

Append to `.dispatch/history.md` after every significant action:

```
YYYY-MM-DD HH:MM — <action taken>
```

This is the audit trail. Never overwrite — only append.

### When to exit

Exit after completing a phase boundary:
- After writing spec and creating draft PR (wait for human)
- After creating implementation PR (wait for human)
- After addressing feedback and pushing (wait for human)

Do NOT loop. Do one phase, update state, exit.
