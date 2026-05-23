# MDS-25: Self-updating — versioning, changelog, and Slack-driven updates

## Problem

Modastack has no mechanism to detect, communicate, or apply updates. Operators
running `modastack start` on a local clone don't know when new features land on
`origin/main`, what changed, or how to update safely. The manager has no way to
tell humans that a new version is available.

## Solution

Add a version-check poller, a Slack notification + approval flow, and a
self-update command. The system periodically compares the local VERSION against
`origin/main`, summarizes what's new from the changelog, messages the operator
on Slack, and — on approval — pulls, reinstalls, and restarts.

## Design

### 1. Versioning

**VERSION file** is the single source of truth (already exists at repo root,
currently `0.2.1`). The pyproject.toml version is secondary — the build system
reads it, but the runtime reads VERSION.

Changes:
- Add `modastack/__version__.py` that reads `VERSION` at import time.
- Update `cli.py` to use `__version__` instead of `importlib.metadata.version()`.
- `pyproject.toml` keeps its own version for packaging; they don't need to be
  in sync at runtime since we install with `pip install -e .` (editable).

### 2. Changelog

**CHANGELOG.md** at repo root. Format:

```markdown
# Changelog

## 0.2.1 — 2026-05-23

- Self-updating: version check, Slack notification, user-approved update
- Slack threading fix for conversations vs proactive updates

## 0.2.0 — 2026-05-20

- Event-driven architecture with persistent manager session
- Linear + GitHub Issues task tracking
```

Maintained manually (or by `/ship`). Each entry is a version header + bullet
list. The update notification extracts the section between the current local
version and the remote version to build the "what's new" summary.

### 3. Version-check poller

New poller: `_poll_version` in `manager/events/pollers.py`.

**Behavior:**
- Runs every **3600s** (1 hour). Also runs once on startup (first tick).
- Executes `git fetch origin main --quiet` in the modastack repo.
- Reads `VERSION` from `origin/main` via `git show origin/main:VERSION`.
- Compares against local `VERSION`.
- If remote > local, reads the changelog diff:
  `git show origin/main:CHANGELOG.md` and extracts entries between local and
  remote versions.
- Pushes event:

```python
bus.push("system.update_available", "system", {
    "current_version": "0.2.1",
    "new_version": "0.3.0",
    "changelog": "- Self-updating support\n- New Slack threading model",
})
```

- Does **not** re-fire if the same version was already announced (tracks
  `last_announced_version` in memory).

**Registration:**
```python
POLLERS = {
    "workers": (_poll_workers, 5),
    "tasks": (_poll_tasks, 30),
    "slack": (_poll_slack, 10),
    "orphans": (_poll_orphans, 60),
    "version": (_poll_version, 3600),
}
```

### 4. Manager handles the event

The manager receives `system/system.update_available` in `pending_events.md`
like any other event. The manager prompt already says to act on events — we
add a section to `manager/prompt.md`:

```markdown
## Update events

When you see `system.update_available`:
1. Post a Slack DM to the operator summarizing what's new.
   Format: "Modastack v{new_version} is available (you're on v{current_version}).
   What's new:\n{changelog}\n\nReply 'update' to apply."
2. Do NOT auto-update. Wait for the human to reply "update" (or similar).
3. When the human replies with approval, run: `modastack self-update`
4. After the command completes, post a confirmation message.
```

No code change needed for the manager to handle this — it already reads events
and acts on them. We just update the prompt to describe the expected behavior.

### 5. Slack approval flow

The operator receives a DM like:

> **Modastack v0.3.0** is available (you're on v0.2.1).
>
> What's new:
> - Self-updating support
> - New Slack threading model
> - Orphan session detection
>
> Reply "update" to apply.

The human replies "update". This arrives as a `slack.dm` or `slack.message`
event. The manager sees it, recognizes the intent, and runs
`modastack self-update`.

### 6. Self-update command

New CLI command: `modastack self-update`

**Implementation** in `modastack/cli.py`:

```python
@main.command("self-update")
def self_update():
    """Pull latest and reinstall modastack."""
```

**Steps:**
1. `git fetch origin main`
2. Check for dirty working tree (`git status --porcelain`). If dirty:
   - Log warning: "Working tree has uncommitted changes."
   - Stash changes: `git stash push -m "modastack-self-update-backup"`
   - Record that we stashed (for rollback).
3. Record current HEAD: `git rev-parse HEAD` (for rollback).
4. `git pull --ff-only origin main`
   - If ff-only fails (diverged history), abort with error message. Don't
     force-merge.
5. `pip install -e .` (reinstall from the updated source).
6. Compare new VERSION vs old VERSION. Log: "Updated from v0.2.1 to v0.3.0".
7. If stashed, pop: `git stash pop`.
8. Print success message with new version.

**Restart behavior:**
- The self-update command does NOT restart the manager itself.
- The manager (which invoked `modastack self-update` via bash) will see the
  command succeed and post a confirmation to Slack.
- On next `modastack start` (or if the consumer loop detects the process
  exited), the updated code runs.
- For a live restart: the manager can run `modastack restart` (kill the
  current manager session, then the consumer loop in `run()` detects
  `is_alive() == False` and calls `start_or_resume()` — which relaunches
  with the updated code).

### 7. Rollback

New CLI command: `modastack rollback`

**Steps:**
1. Read the saved pre-update HEAD from `~/.modastack/update_state.json`:
   ```json
   {
     "pre_update_head": "abc123",
     "pre_update_version": "0.2.1",
     "updated_at": "2026-05-23T14:30:00",
     "stashed": false
   }
   ```
2. `git reset --hard {pre_update_head}`
3. `pip install -e .`
4. Log: "Rolled back to v0.2.1"

The update state file is written by `self-update` before pulling.

### 8. Startup version check

In `consumer.py`'s `run()`, after starting pollers, push an initial version
check event so the operator gets notified immediately (not after waiting 1
hour for the first poll):

```python
# Trigger initial version check
threading.Thread(target=_poll_version, args=(0,), daemon=True).start()
```

This is just the poller running once with interval=0 (exits after one check
instead of looping). Alternatively, extract the check logic into a helper and
call it directly.

## File changes

| File | Change |
|------|--------|
| `CHANGELOG.md` | **New.** Initial changelog with existing versions. |
| `modastack/__version__.py` | **New.** Reads VERSION file, exports `__version__`. |
| `modastack/cli.py` | Add `self-update` and `rollback` commands. Use `__version__`. |
| `manager/events/pollers.py` | Add `_poll_version` poller + register in POLLERS. |
| `manager/prompt.md` | Add "Update events" section. |
| `manager/events/consumer.py` | Add initial version check on startup. |

## Not in scope

- **Auto-update without approval.** Always requires human confirmation.
- **Multi-repo version tracking.** Only tracks the modastack repo itself.
- **Release tags / GitHub releases.** Uses VERSION file + main branch comparison.
- **Update channels** (stable/beta). Single channel: `origin/main`.
- **Remote modastack instances.** This is for the local clone only.

## Edge cases

| Case | Handling |
|------|----------|
| Dirty working tree | Stash before pull, pop after. Warn in Slack message. |
| Diverged history | `--ff-only` fails → abort, tell operator to manually reconcile. |
| `pip install` fails | Log error, suggest rollback. Don't restart. |
| No internet / fetch fails | Poller catches exception, logs, retries next interval. |
| Same version re-announced | Track `last_announced_version`, skip duplicate events. |
| Manager invokes self-update while processing events | Self-update runs in a subprocess; manager waits for exit code. |
| Worktrees exist during update | `git pull --ff-only` on main doesn't affect worktrees. Safe. |

## Test plan

- **Unit: `_poll_version`** — mock `subprocess.run` for git commands, verify
  event is pushed when remote > local, not pushed when equal.
- **Unit: `self-update` command** — mock git + pip subprocess calls, verify
  correct sequence and error handling for dirty tree / diverged history.
- **Unit: changelog parsing** — extract entries between two versions from a
  sample CHANGELOG.md.
- **Integration: full flow** (manual) — push a version bump to a test remote,
  verify Slack notification arrives, reply "update", verify update completes.
