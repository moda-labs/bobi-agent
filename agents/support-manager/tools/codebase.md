# Codebase (high-level investigation)

When triaging a signal you take a **high-level** look at the product
codebase (the path in `workspace/support-context.md`) to gather context for an
engineer. You are not fixing the bug and
not doing a full root-cause — you are routing with enough context that the
engineering agent can start fast. The repo path and entry points come from
`workspace/support-context.md`.

## Read-only, always

Never edit, never commit, never create a branch or worktree in the
codebase. The fix is the engineering agent's job; filing the ticket is the
handoff. Read with `git -C <repo> ...`, `grep`/ripgrep, and file reads
only.

## A good high-level pass (time-boxed)

1. **Locate.** From the error/stack trace or the symptom, find the
   file/function involved. Start from the entry points named in the
   context file so you land in user-facing logic, not a vendored library.
   ```bash
   grep -rn "<error message or symbol>" <repo>/<src dirs>
   ```
2. **Read around it.** Read enough of the suspect function to understand
   what it does and a plausible failure mode.
3. **Check recent changes.** A regression usually has a culprit commit.
   ```bash
   git -C <repo> log --oneline -15 -- <suspect path>
   git -C <repo> log --oneline -15           # recent activity overall
   ```
   Name the commit if one plausibly introduced the behavior.
4. **Summarize, do not solve.** Capture: the affected module, the suspect
   file(s)/function, any culprit commit, a one-line hypothesis, and a rough
   effort read (small / medium / large / unclear).

## When the cause is unclear

That is an acceptable outcome. Record "cause unclear, needs engineer
investigation" with whatever you did narrow down (the surface, the entry
point, what you ruled out). Do not spend a long time — the ticket exists
so an engineer can take it from here.

## Feeds the ticket

Everything from this pass goes into the Linear description (suspect files,
culprit commit, hypothesis, effort) so the engineering agent starts from a
real brief, not just a symptom.
