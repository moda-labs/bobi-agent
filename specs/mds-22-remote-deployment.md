# MDS-22: Remote deployment — auto-clone repos, worktree lifecycle, Slack-driven setup

## Problem & Solution

**Problem:** Modastack assumes repos already exist on disk. On a fresh remote box (EC2, Mac mini), an operator must SSH in, manually clone each repo, run `modastack setup`, and register it. There's no way to manage repos through Slack after initial deployment. Additionally, engineer worktrees accumulate forever on disk, and main branches go stale because nobody runs `git pull` on headless boxes.

**Who it solves for:** Operators deploying modastack to remote machines who want to manage everything via Slack after a one-time SSH setup (install tooling, auth GitHub, auth Claude, set Slack tokens).

**Solution:** Eight changes that collectively make modastack deployable and self-maintaining on remote boxes:

1. Config supports `{remote, path}` repo entries (backward-compat with bare paths)
2. `modastack register` accepts `org/repo` — clones, sets up, and registers
3. Auto-clone missing repos on startup
4. Main branch sync before engineer spawn
5. Worktree + branch cleanup when task moves to Done
6. Git identity configuration during `modastack init`
7. Manager learns to set up repos via Slack commands
8. Deploy script simplified

## Scope

**In:**
- Config format: `{remote, path}` repo entries with backward compatibility
- CLI: `register` accepts `org/repo`, `init` sets git identity
- Consumer: auto-clone on startup
- Session: main-branch sync before spawn, worktree cleanup
- Manager prompt: repo setup instructions
- Deploy script: simplified post-install
- Tests for config and CLI changes

**Out:**
- GitHub App authentication (stays with `gh auth login`)
- Multi-machine coordination
- S3/artifact storage for worktrees
- Custom deploy pipelines
- Automated `gh auth login` (interactive, one-time)

## Technical Approach

### 1. Config format — `modastack/config.py`

Add a `RepoEntry` dataclass alongside the existing config:

```python
@dataclass
class RepoEntry:
    path: Path
    remote: str = ""  # e.g. "moda-labs/bettertab"
```

Change `GlobalConfig.repos` from `list[Path]` to `list[RepoEntry]`.

**Parsing (backward compat):** In `GlobalConfig.load()`, handle both formats:
```yaml
# Old format — still works
repos:
  - /Users/zach/dev/bettertab

# New format
repos:
  - remote: moda-labs/bettertab
    path: ~/.modastack/repos/bettertab
```

Parsing logic:
- If entry is a string → `RepoEntry(path=Path(entry).expanduser())`
- If entry is a dict → `RepoEntry(path=Path(entry["path"]).expanduser(), remote=entry.get("remote", ""))`

**Serialization:** `GlobalConfig.save()` writes the dict format for entries with a remote, bare strings for local-only entries. This preserves readability for existing local-dev configs.

**Downstream consumers:** Add a convenience property `GlobalConfig.repo_paths` → `list[Path]` that returns `[e.path for e in self.repos]`. Existing code that only needs paths uses `config.repo_paths` — zero call-site churn. Only new code that needs the remote (register, auto-clone) accesses `config.repos` directly.

### 2. `modastack register` accepts GitHub remote — `modastack/cli.py`

Change the `register` command signature:
```python
@main.command()
@click.argument("target")  # path OR org/repo
def register(target: str):
```

Detection: use a regex `^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$` — exactly one slash, alphanumeric segments. This correctly distinguishes `moda-labs/bettertab` from relative paths like `../my-repo` or `./my-repo`.

For `org/repo` format:
1. Determine clone path: `~/.modastack/repos/<repo-name>/`
2. Clone: `gh repo clone <org/repo> <clone-path>`
3. Run full setup (call shared `full_setup(repo_path)` — see below)
4. Add `RepoEntry(path=clone_path, remote=target)` to global config

For local paths: existing behavior, but wrap in `RepoEntry(path=resolved)`.

**Why `gh repo clone` instead of `git clone`?** `gh` handles auth automatically — it uses the stored OAuth token, handles SSH vs HTTPS transparently, and works with private repos the user has access to.

### 3. Auto-clone on startup — `manager/events/consumer.py`

Add a function `_ensure_repos()` called at the top of `run()`, before `start_or_resume()`:

```python
def _ensure_repos():
    config = GlobalConfig.load()
    for entry in config.repos:
        if entry.path.exists():
            continue
        if not entry.remote:
            log.warning(f"Repo missing and no remote configured: {entry.path}")
            continue
        log.info(f"Cloning {entry.remote} → {entry.path}")
        entry.path.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["gh", "repo", "clone", entry.remote, str(entry.path)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            log.error(f"Clone failed: {result.stderr}")
            continue
        # Run full setup (config, skills, Linear bootstrap)
        from modastack.setup import full_setup
        full_setup(entry.path)
        log.info(f"Cloned and set up: {entry.remote}")
```

This makes `modastack start` idempotent — restart/redeploy just works.

**Shared setup function:** Extract the setup logic from `cli.py`'s `setup` command into `modastack/setup.py` as `full_setup(repo_path: Path, credential_name: str = None)`. This function: generates `.modastack.yaml`, installs skill symlinks, and bootstraps the Linear board (if credentials exist). Both the CLI `setup` command and `_ensure_repos()` call it.

### 4. Main branch sync before engineer spawn — `modastack/session.py`

Add a function `sync_main_branch(repo_path: Path)`:

```python
def sync_main_branch(repo_path: Path) -> bool:
    # Get the remote's default branch (main, master, develop, etc.)
    ref_result = subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
        capture_output=True, text=True, cwd=repo_path,
    )
    if ref_result.returncode == 0:
        default_branch = ref_result.stdout.strip().split("/")[-1]
    else:
        default_branch = "main"

    result = subprocess.run(
        ["git", "fetch", "origin"],
        capture_output=True, text=True, cwd=repo_path,
    )
    if result.returncode != 0:
        log.warning(f"git fetch failed in {repo_path}: {result.stderr}")
        return False

    result = subprocess.run(
        ["git", "reset", "--hard", f"origin/{default_branch}"],
        capture_output=True, text=True, cwd=repo_path,
    )
    if result.returncode != 0:
        log.warning(f"git reset failed in {repo_path}: {result.stderr}")
        return False

    log.info(f"Synced {repo_path.name} to origin/{default_branch}")
    return True
```

Call this from `spawn_session()` before creating the worktree. The `cwd` parameter already points to the repo root.

**Safety:** `git reset --hard` on the main branch in the repo root is safe because:
- On remote boxes, nobody works on main directly — all work is in worktrees
- On local dev, the repo root is also the worktree, but engineers shouldn't have uncommitted work on main
- Worktrees are isolated — `reset --hard` on main doesn't affect them

**Default branch detection:** Use `git symbolic-ref refs/remotes/origin/HEAD` to get the actual default branch name (main vs master vs develop). Fall back to "main".

### 5. Worktree cleanup on Done — `modastack/session.py`

Add a function `cleanup_worktree(issue_id: str, repo_path: Path)`:

```python
def cleanup_worktree(issue_id: str, repo_path: Path) -> None:
    branch = f"agent/{issue_id.lower()}"
    worktree_path = repo_path / "worktrees" / issue_id.lower()

    # Kill tmux session first if still running
    if session_exists(issue_id):
        kill_session(issue_id)
        time.sleep(1)

    # Remove worktree
    if worktree_path.exists():
        result = subprocess.run(
            ["git", "worktree", "remove", str(worktree_path), "--force"],
            capture_output=True, text=True, cwd=repo_path,
        )
        if result.returncode != 0:
            log.warning(f"Worktree removal failed: {result.stderr}")
        else:
            log.info(f"Removed worktree: {worktree_path}")

    # Delete the branch
    subprocess.run(
        ["git", "branch", "-D", branch],
        capture_output=True, text=True, cwd=repo_path,
    )

    # Clean up session ID file
    saved_id_path = SESSION_IDS_DIR / f"{issue_id}.id"
    if saved_id_path.exists():
        saved_id_path.unlink()
```

**When to call it:** The manager calls this when moving a task to Done (after PR merge). This is triggered by the manager's decision logic — the manager already handles `move_linear_issue` actions. We add cleanup as part of the "close" flow.

The manager prompt already describes what to do when a PR is merged — we add instructions to call `cleanup_worktree()` directly as part of the "close" flow. No wrapper needed.

### 6. Git identity on init — `modastack/cli.py`

Extend the `init` command to configure git identity:

```python
@main.command()
@click.option("--non-interactive", is_flag=True, envvar="CI")
@click.option("--git-name", default="Modabot")
@click.option("--git-email", default=None)
def init(non_interactive, git_name, git_email):
    # ... existing init logic ...

    # Configure git identity
    current_name = subprocess.run(
        ["git", "config", "--global", "user.name"],
        capture_output=True, text=True,
    ).stdout.strip()

    if not current_name or non_interactive:
        subprocess.run(["git", "config", "--global", "user.name", git_name])
        email = git_email or f"modabot@modastack.dev"
        subprocess.run(["git", "config", "--global", "user.email", email])
        click.echo(f"Git identity: {git_name} <{email}>")
    else:
        click.echo(f"Git identity already set: {current_name}")
```

On remote boxes with `--non-interactive`, this sets a default Modabot identity. On local dev, it skips if git is already configured.

### 7. Manager prompt — repo setup via Slack — `manager/prompt.md`

Add a new section to the manager prompt:

```markdown
## Repo setup via Slack

When a human asks you to set up a new repo (e.g., "set up moda-labs/bettertab"
or "add the bettertab repo"):

1. Run: `modastack register <org/repo>`
   This clones the repo, generates .modastack.yaml, installs skills,
   and registers it in the global config.

2. If it needs a Linear project key, ask the human on Slack.

3. Confirm on Slack: "Cloned <repo>, bootstrapped config, ready to work.
   Linear project: <key>. I'll start picking up `agent`-labeled issues."

If registration fails (auth issue, repo not found), report the error on Slack
and suggest the human check `gh auth status`.
```

### 8. Deploy script — `deploy/setup-ec2.sh`

Simplify the "Next steps" section. Steps 1-3 stay (auth is interactive). Replace steps 4-6:

```
  4. Start modastack:
     tmux new -s modastack
     source ~/modastack/.venv/bin/activate
     modastack start --webhooks
     # Ctrl-B D to detach

  5. Set up repos via Slack (no more SSH needed):
     DM Modabot: "set up moda-labs/bettertab"
     DM Modabot: "set up advisor360/DATS-Tesseract"
```

## Design Decisions

| Decision | Choice | Alternative | Why |
|---|---|---|---|
| Clone tool | `gh repo clone` | `git clone` | `gh` handles auth (OAuth, SSH) transparently for private repos |
| Config format | Dict entries with bare-path fallback | Always dict | Backward compat — existing configs just work |
| Main sync method | `git fetch + reset --hard` | `git pull` | Pull can fail on merge conflicts; reset is idempotent |
| Worktree cleanup trigger | Manager decision on Done | Automatic on PR merge webhook | Manager already handles Done transitions; keep logic centralized |
| Default clone path | `~/.modastack/repos/<name>/` | Configurable | Simple, predictable. User can override via `path:` in config |
| Git identity scope | `--global` | Per-repo | Remote boxes run one identity; local dev already has git configured |

## Verification Plan

### Level 1 — Unit tests

**`tests/test_config.py`:**
- `test_repo_entry_from_string` — bare path string → `RepoEntry(path=Path(...))`
- `test_repo_entry_from_dict` — `{remote, path}` dict → `RepoEntry(path=..., remote=...)`
- `test_global_config_mixed_repos` — config with both bare paths and dict entries loads correctly
- `test_global_config_roundtrip_with_remotes` — save + load preserves remote info
- `test_repo_paths_property` — `GlobalConfig.repo_paths` returns `list[Path]`

**`tests/test_cli.py`:**
- `test_register_detects_remote` — `org/repo` format is detected vs local path
- `test_register_local_path_still_works` — existing behavior preserved

**`tests/test_session.py`** (new):
- `test_cleanup_worktree_removes_branch` — mock subprocess, verify git commands
- `test_cleanup_worktree_kills_session_first` — verify tmux kill before git cleanup
- `test_sync_main_branch_fetches_and_resets` — mock subprocess, verify commands

### Level 2 — Integration (manual, not in CI)

- Register a real repo via `modastack register org/repo` on a test machine
- Verify auto-clone on `modastack start` with a missing repo path
- Verify worktree cleanup after manually moving a task to Done

### Level 3 — End-to-end (manual QA)

- Fresh EC2 instance: run `setup-ec2.sh`, auth Claude + GitHub, start modastack
- DM Modabot on Slack: "set up moda-labs/bettertab"
- Verify repo appears in config, Linear board bootstrapped
- Create an `agent`-labeled issue, verify engineer spawns
- Merge the PR, verify worktree cleaned up

## Implementation Plan

Ordered by dependency — each step builds on the previous:

### Step 1: Config format (`modastack/config.py`)
- Add `RepoEntry` dataclass
- Update `GlobalConfig.repos` type to `list[RepoEntry]`
- Update `load()` parsing for backward compat
- Update `save()` serialization
- Add `repo_paths` convenience property
- Update tests in `tests/test_config.py`

### Step 2: Downstream consumers use `repo_paths`
- `manager/events/pollers.py`: use `config.repo_paths` in `_poll_linear()` and `_poll_orphans()`
- `manager/session.py`: use `config.repo_paths[0]` in `start_or_resume()`
- `modastack/cli.py`: use `config.repo_paths` in `repos` and `setup` commands

### Step 3: Extract `full_setup()` into `modastack/setup.py`
- Move setup logic (generate config, install skills, bootstrap Linear) from `cli.py` into `full_setup(repo_path)`
- CLI `setup` command calls `full_setup()` instead of inline logic

### Step 4: CLI `register` accepts org/repo (`modastack/cli.py`)
- Change `register` to accept `target` string (not `Path(exists=True)`)
- Add `org/repo` detection via regex `^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$`
- Clone via `gh repo clone`, call `full_setup()`, register in config
- Add tests

### Step 5: Auto-clone on startup (`manager/events/consumer.py`)
- Add `_ensure_repos()` function calling `full_setup()` after clone
- Call it at top of `run()`

### Step 6: Main branch sync (`modastack/session.py`)
- Add `sync_main_branch()` using `git symbolic-ref refs/remotes/origin/HEAD`
- Call from `spawn_session()` before creating the tmux session

### Step 7: Worktree cleanup (`modastack/session.py`)
- Add `cleanup_worktree()` function (no wrapper)

### Step 8: Git identity on init (`modastack/cli.py`)
- Add `--git-name` and `--git-email` options to `init`
- Set git config when not already configured

### Step 9: Manager prompt + deploy script
- Add repo setup instructions to `manager/prompt.md`
- Simplify `deploy/setup-ec2.sh` post-install steps

### Step 10: Tests (alongside each step, listed here for tracking)

**`tests/test_config.py`:**
- `test_repo_entry_from_string` — bare path string → RepoEntry
- `test_repo_entry_from_dict` — {remote, path} dict → RepoEntry
- `test_global_config_mixed_repos` — both formats load correctly
- `test_global_config_roundtrip_with_remotes` — save + load preserves remote
- `test_repo_paths_property` — returns list[Path]

**`tests/test_cli.py`:**
- `test_register_detects_remote` — org/repo format detected
- `test_register_rejects_relative_path_as_remote` — ../foo not treated as remote
- `test_register_local_path_still_works` — existing behavior
- `test_register_clone_success` — mock gh, verify full_setup called
- `test_register_clone_failure` — mock gh failure, verify error message
- `test_init_sets_git_identity` — non-interactive mode
- `test_init_skips_existing_identity` — identity already configured

**`tests/test_session.py`** (new):
- `test_sync_main_branch_fetches_and_resets` — mock subprocess
- `test_sync_main_branch_fetch_failure` — returns False on fetch error
- `test_sync_main_branch_detects_default_branch` — symbolic-ref parsing
- `test_cleanup_worktree_kills_session_first` — verify ordering
- `test_cleanup_worktree_removes_branch` — verify git commands
- `test_cleanup_worktree_session_already_dead` — skip kill gracefully
- `test_cleanup_worktree_missing_worktree` — skip removal gracefully
- `test_cleanup_worktree_removal_fails` — logs warning, continues

**`tests/test_consumer.py`** (new):
- `test_ensure_repos_skips_existing` — path exists, no clone
- `test_ensure_repos_clones_missing_with_remote` — mock gh, verify
- `test_ensure_repos_warns_missing_no_remote` — logs warning
- `test_ensure_repos_handles_clone_failure` — logs error, continues

## NOT in scope

- GitHub App auth / automatic token management — stays with `gh auth login`
- Multi-machine coordination (shared state, leader election) — single-box only
- S3/remote artifact storage for worktree contents
- Automated SSH key management or PAT rotation
- Repo-level access control (who can register what) — trust-based
- Configurable clone paths per repo (deferred — `~/.modastack/repos/<name>/` is sufficient)

## What already exists

- `modastack setup` handles the full setup flow (config, skills, Linear, credentials) — we extract into `full_setup()` for reuse
- `kill_session()` already handles tmux cleanup — `cleanup_worktree()` extends it with git cleanup
- `GlobalConfig.load()/save()` already round-trips YAML — we extend the parser, not replace it
- `generate_dispatch_yaml()` in `setup.py` already generates `.modastack.yaml` from repo inspection

## Failure modes

| Codepath | Failure scenario | Test? | Error handling? | User visible? |
|---|---|---|---|---|
| `_ensure_repos` clone | `gh` not authed, repo private | Yes | Logs error, skips repo | Log only (no Slack) |
| `sync_main_branch` fetch | Network down, remote unreachable | Yes | Logs warning, returns False, spawn continues | No — engineer starts with stale main |
| `sync_main_branch` reset | Merge conflict (shouldn't happen on remote) | Yes | Logs warning, returns False | No |
| `cleanup_worktree` remove | Worktree has locked files | Yes | `--force` flag handles most cases, logs warning | No |
| `register` clone | Invalid org/repo format, 404 | Yes | Error message printed | CLI output |
| `full_setup` skills install | modastack repo moved, symlinks broken | No | Skills dir check exists | CLI output |
