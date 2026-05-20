# Source Control Conventions

This documents our branching, commit, and PR conventions. For the
mechanical git and GitHub CLI commands, see `tools/git` and `tools/github`.

## Branching conventions

- Branch name: `agent/<issue-id-lowercase>` (e.g., `agent/bet-10`)
- One branch per ticket
- Branch from `main` (or whatever the default branch is)

## Commit conventions

- Prefix with the ticket ID: `[BET-10] feat: add rate limiting`
- Format: `[ISSUE-ID] type: description`
- Types: `feat`, `fix`, `docs`, `chore`, `refactor`, `test`
- One logical change per commit

## PR title format

Always: `[ISSUE-ID] type: description`

Examples:
- `[BET-10] feat: add rate limiting to API`
- `[AGD-22] fix: move LOG_DIR constant to config.py`
- `[BET-11] docs: rewrite README for new architecture`
