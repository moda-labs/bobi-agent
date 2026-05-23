---
issue_id: MDS-22
title: "Remote deployment: auto-clone repos, worktree lifecycle, Slack-driven setup"
worktree: /Users/zkozick/dev/modastack/worktrees/mds-22
branch: agent/mds-22
phase: triage_complete
complexity: large
needs_spec: true
---

## Issue
Remote deployment support: auto-clone repos from GitHub URL, worktree cleanup on Done, main branch sync before spawn, Slack-driven repo setup, git identity config.

## Codebase understanding

**Config layer** (`modastack/config.py`): `GlobalConfig.repos` is `list[Path]` — loaded from bare strings in YAML. `save()` serializes back as `list[str]`. `RepoConfig` handles per-repo `.modastack.yaml`. The pattern is dataclass + classmethod `load()`/`from_file()` with yaml round-trip.

**CLI** (`modastack/cli.py`): Click-based. `register` takes `Path(exists=True)` — currently rejects non-existent paths. `setup` generates `.modastack.yaml`, stores credentials, installs skill symlinks, bootstraps Linear board.

**Consumer** (`manager/events/consumer.py`): `run()` is the main event loop. Starts manager session, starts webhooks/pollers, then loops draining the bus and injecting events. No startup repo validation currently.

**Pollers** (`manager/events/pollers.py`): Four pollers in background threads. `_poll_linear()` iterates `global_config.repos` — skips if `not repo_path.exists()`. No main-branch sync anywhere.

**Session management** (`modastack/session.py`): `spawn_session(issue_id, cwd)` creates tmux sessions for engineers. No worktree cleanup on session kill/completion. `kill_session()` only kills the tmux session.

**Manager prompt** (`manager/prompt.md`): Defines actions (spawn_worker, kill_worker, etc.) but has no repo setup instructions. Manager doesn't know how to clone or register repos.

**Deploy script** (`deploy/setup-ec2.sh`): Manual 6-step post-setup including "Register repos" via SSH. No Slack-driven alternative.

## Triage
Large: 8 sub-features across 7+ files. Config format change (data model), CLI changes, new startup logic in consumer, new lifecycle hooks in session management, manager prompt additions, deploy script updates. All sub-features serve a single goal (remote deployment without SSH) and belong in one PR. Self-modification guardrail applies — spec mandatory.

## Relevant files
- `modastack/config.py` — Add `RepoEntry` type supporting `{remote, path}`, backward-compat parsing
- `modastack/cli.py` — `register` accepts `org/repo`, `init` configures git identity
- `manager/events/consumer.py` — Auto-clone missing repos before event loop
- `manager/events/pollers.py` or `modastack/session.py` — Main branch sync before spawn
- `modastack/session.py` — Worktree cleanup function
- `manager/prompt.md` — Repo setup instructions for Slack-driven bootstrapping
- `deploy/setup-ec2.sh` — Simplify post-install to "DM Modabot on Slack"
- `tests/test_config.py` — Tests for new config format
- `tests/test_cli.py` — Tests for register with org/repo

## Risks and edge cases
- Config backward compatibility: existing bare-path configs must keep working
- `gh` CLI must be authed on the remote box (prerequisite, not automated)
- Clone failures (private repo, no access) need graceful error handling
- `git reset --hard` on main is destructive if someone has uncommitted work there (safe on remote-only boxes, risky in local dev)
- Worktree removal must handle case where worktree has uncommitted changes
- Race condition: cleanup running while engineer session still has file handles open

## Next
Run /spec to write a detailed implementation spec before any code changes.
