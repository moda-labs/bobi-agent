# MDS-22: Remote deployment — auto-clone repos, worktree lifecycle, Slack-driven setup

## Problem & Solution

**Problem:** Modastack assumes repos already exist on disk. On a fresh remote box (EC2, Mac mini), an operator must SSH in, manually clone each repo, run `modastack setup`, and register it. There's no way to manage repos through Slack after initial deployment. Additionally, engineer worktrees accumulate forever on disk, and main branches go stale because nobody runs `git pull` on headless boxes.

**Who it solves for:** Operators deploying modastack to remote machines who want to manage everything via Slack after a one-time SSH setup (install tooling, auth GitHub, auth Claude, set Slack tokens).

**Solution:** Eight changes that collectively make modastack deployable and self-maintaining on remote boxes:

1. Consolidate all config into `~/.modastack/config.yaml` — eliminate per-repo `.modastack.yaml`
2. `modastack register` accepts `org/repo` — clones, detects settings, registers
3. Auto-clone missing repos on startup
4. Main branch sync before engineer spawn
5. Worktree + branch cleanup when task moves to Done
6. Git identity configuration during `modastack init`
7. Manager learns to set up repos via Slack commands
8. Deploy script simplified

## Scope

**In:**
- Config consolidation: merge `RepoConfig` fields into `RepoEntry` in `GlobalConfig`
- Delete `RepoConfig` class and `.modastack.yaml` generation
- CLI: `register` accepts `org/repo`, `init` sets git identity
- Consumer: auto-clone on startup
- Session: main-branch sync before spawn, worktree cleanup
- Manager prompt: repo setup instructions
- Deploy script: simplified post-install
- Migrate all consumers (pollers, webhooks, scanner) from `RepoConfig` to `RepoEntry`
- Tests for config and CLI changes

**Out:**
- GitHub App authentication (stays with `gh auth login`)
- Multi-machine coordination
- S3/artifact storage for worktrees
- Custom deploy pipelines
- Automated `gh auth login` (interactive, one-time)

## Analysis: eliminating per-repo config files

The current system has two config layers:
- `~/.modastack/config.yaml` (global) — Slack tokens, webhook config, registered repo paths
- `<repo>/.modastack.yaml` (per-repo) — Linear project key, credentials ref, test command, trigger labels, etc.

**What `.modastack.yaml` contains and where it's actually consumed:**

| Field | Python consumer | Auto-detectable? |
|---|---|---|
| `linear.project` | `scanner.py`, `pollers.py`, `webhook_server.py` | No — must be provided |
| `credentials` | `pollers.py` via `get_credentials()` | No — must be provided |
| `linear.trigger_labels` | Manager prompt only (not Python code) | Defaults work (`["agent"]`) |
| `linear.skip_labels` | Manager prompt only | Defaults work |
| `verify.test_command` | Engineer skill docs only | Yes — `setup.py` already detects it |
| `agent.max_parallel` | Not consumed in code | Default works (`2`) |
| `verify.review_required` | Not consumed in code | Default works (`true`) |
| `verify.auto_merge` | Not consumed in code | Default works (`false`) |
| `agent.skills` | Not consumed in code | Yes — `setup.py` already detects it |
| `context` | Not consumed anywhere | N/A |

**Only 2 fields require human input:** `linear_project` and `credentials`. Everything else is either auto-detected or uses sensible defaults.

**Decision: eliminate `.modastack.yaml`.** Move `linear_project` and `credentials` into `RepoEntry` in the global config. Auto-detected values (test command, skills) are computed at runtime when needed, not stored. Benefits:

1. No modastack-specific files pollute target repos
2. One config file instead of two — simpler mental model
3. `modastack setup` doesn't need write access to the target repo
4. Remote deployment becomes: register + done
5. No backward compatibility concern — there's only one format

The skill symlinks (`.claude/skills/`) still get installed into target repos — those are Claude Code's native mechanism, not modastack config.

## Technical Approach

### 1. Config consolidation — `modastack/config.py`

Replace `list[Path]` repos with `list[RepoEntry]`. Absorb the fields from `RepoConfig` that are actually used. Delete `RepoConfig`.

```python
@dataclass
class RepoEntry:
    path: Path
    remote: str = ""                            # e.g. "moda-labs/bettertab"
    linear_project: str = ""                    # e.g. "BT"
    credentials: str = "default"                # key into credentials.yaml
    trigger_labels: list[str] = field(default_factory=lambda: ["agent"])
    skip_labels: list[str] = field(default_factory=lambda: ["blocked", "human-only"])

    def get_credentials(self) -> dict[str, str]:
        creds = Credentials.load()
        return creds.get(self.credentials)
```

Change `GlobalConfig.repos` from `list[Path]` to `list[RepoEntry]`.

**Parsing:** Always dict format:
```yaml
repos:
  - remote: moda-labs/bettertab
    path: ~/.modastack/repos/bettertab
    linear_project: BT
    credentials: default
```

**Serialization:** `GlobalConfig.save()` writes all entries as dicts.

**Convenience property:** `GlobalConfig.repo_paths` -> `list[Path]` for code that only needs paths.

**Lookup helper:** `GlobalConfig.get_repo(path: Path) -> RepoEntry | None` for code that needs the full entry for a given repo path.

**Delete:** `RepoConfig` class, `RepoConfig.from_file()`, all references to `.modastack.yaml` in config.py.

### 2. `modastack register` accepts GitHub remote — `modastack/cli.py`

Change the `register` command signature:
```python
@main.command()
@click.argument("target")  # path OR org/repo
@click.option("--linear-project", default="", help="Linear project key (e.g. BT)")
@click.option("--credentials", default="default", help="Credential set name")
def register(target: str, linear_project: str, credentials: str):
```

Detection: use a regex `^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$` — exactly one slash, alphanumeric segments. This correctly distinguishes `moda-labs/bettertab` from relative paths like `../my-repo` or `./my-repo`.

For `org/repo` format:
1. Determine clone path: `~/.modastack/repos/<repo-name>/`
2. Clone: `gh repo clone <org/repo> <clone-path>`
3. Auto-detect `linear_project` if not provided (call `detect_linear_project()`)
4. Install skill symlinks
5. Add `RepoEntry(path=clone_path, remote=target, linear_project=..., credentials=...)` to global config

For local paths:
1. Resolve to absolute path
2. Auto-detect `linear_project` if not provided
3. Install skill symlinks
4. Add `RepoEntry(path=resolved, linear_project=..., credentials=...)` to global config

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
        log.info(f"Cloning {entry.remote} -> {entry.path}")
        entry.path.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["gh", "repo", "clone", entry.remote, str(entry.path)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            log.error(f"Clone failed: {result.stderr}")
            continue
        install_skill_symlinks(entry.path)
        log.info(f"Cloned and set up: {entry.remote}")
```

This makes `modastack start` idempotent — restart/redeploy just works.

**Shared setup function:** `full_setup()` in `modastack/setup.py` is simplified — it installs skill symlinks and bootstraps the Linear board (if credentials exist). It no longer generates `.modastack.yaml`. Both the CLI `register` command and `_ensure_repos()` call it.

### 4. Main branch sync before engineer spawn — `modastack/session.py`

Add a function `sync_main_branch(repo_path: Path)`:

```python
def sync_main_branch(repo_path: Path) -> bool:
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

Call this from `spawn_session()` before creating the worktree.

**Safety:** `git reset --hard` on the main branch in the repo root is safe because:
- On remote boxes, nobody works on main directly — all work is in worktrees
- Worktrees are isolated — `reset --hard` on main doesn't affect them

**Default branch detection:** Use `git symbolic-ref refs/remotes/origin/HEAD` to get the actual default branch name. Fall back to "main".

### 5. Worktree cleanup on Done — `modastack/session.py`

Add a function `cleanup_worktree(issue_id: str, repo_path: Path)`:

```python
def cleanup_worktree(issue_id: str, repo_path: Path) -> None:
    branch = f"agent/{issue_id.lower()}"
    worktree_path = repo_path / "worktrees" / issue_id.lower()

    if session_exists(issue_id):
        kill_session(issue_id)
        time.sleep(1)

    if worktree_path.exists():
        result = subprocess.run(
            ["git", "worktree", "remove", str(worktree_path), "--force"],
            capture_output=True, text=True, cwd=repo_path,
        )
        if result.returncode != 0:
            log.warning(f"Worktree removal failed: {result.stderr}")
        else:
            log.info(f"Removed worktree: {worktree_path}")

    subprocess.run(
        ["git", "branch", "-D", branch],
        capture_output=True, text=True, cwd=repo_path,
    )

    saved_id_path = SESSION_IDS_DIR / f"{issue_id}.id"
    if saved_id_path.exists():
        saved_id_path.unlink()
```

**When to call it:** The manager calls this when moving a task to Done (after PR merge). The manager prompt gets updated with cleanup instructions.

### 6. Git identity on init — `modastack/cli.py`

Extend the `init` command to configure git identity:

```python
@main.command()
@click.option("--non-interactive", is_flag=True, envvar="CI")
@click.option("--git-name", default="Modabot")
@click.option("--git-email", default=None)
def init(non_interactive, git_name, git_email):
    # ... existing init logic ...

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

1. Run: `modastack register <org/repo> --linear-project <KEY>`
   This clones the repo, installs skills, and registers it in the global config.

2. If you don't know the Linear project key, ask the human on Slack.

3. Confirm on Slack: "Cloned <repo>, ready to work.
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

### 9. Migrate consumers from `RepoConfig` to `RepoEntry`

**`manager/events/pollers.py`** — `_poll_linear()` and `_poll_orphans()`:
- Replace `RepoConfig.from_file(repo_path)` with `config.get_repo(repo_path)`
- Access `entry.linear_project`, `entry.get_credentials()` directly
- Remove `FileNotFoundError` try/catch (no file to be missing)

**`manager/events/webhook_server.py`** — Linear webhook handler:
- Replace `RepoConfig.from_file(repo_path)` with `config.get_repo(repo_path)`
- Build `configured_projects` from `entry.linear_project` for each repo entry

**`modastack/scanner.py`**:
- Change signature from `scan_linear_all_active(api_key, repo_config: RepoConfig)` to `scan_linear_all_active(api_key, linear_project: str)`
- Only uses `repo_config.linear_project` anyway — pass the string directly

## Design Decisions

| Decision | Choice | Alternative | Why |
|---|---|---|---|
| Per-repo config | Eliminate `.modastack.yaml`, centralize in global config | Keep per-repo files | Only 2 fields need human input; everything else auto-detected or defaults. No modastack files in target repos. |
| Config format | Dict entries only, no bare-path support | Bare-path fallback | Clean break — no backward compat needed. One format, no ambiguity. |
| Clone tool | `gh repo clone` | `git clone` | `gh` handles auth (OAuth, SSH) transparently for private repos |
| Main sync method | `git fetch + reset --hard` | `git pull` | Pull can fail on merge conflicts; reset is idempotent |
| Worktree cleanup trigger | Manager decision on Done | Automatic on PR merge webhook | Manager already handles Done transitions; keep logic centralized |
| Default clone path | `~/.modastack/repos/<name>/` | Configurable | Simple, predictable. User can override via `path:` in config |
| Git identity scope | `--global` | Per-repo | Remote boxes run one identity; local dev already has git configured |
| Auto-detected settings | Compute at runtime, don't store | Store in config on register | Test command and skills change as repos evolve; runtime detection stays current |

## Verification Plan

### Level 1 — Unit tests

**`tests/test_config.py`:**
- `test_repo_entry_from_dict` — `{remote, path, linear_project}` dict -> `RepoEntry`
- `test_repo_entry_defaults` — missing optional fields get defaults
- `test_global_config_repos_roundtrip` — save + load preserves all RepoEntry fields
- `test_repo_paths_property` — `GlobalConfig.repo_paths` returns `list[Path]`
- `test_get_repo_found` — `GlobalConfig.get_repo(path)` returns matching entry
- `test_get_repo_not_found` — returns None for unknown path
- `test_repo_config_deleted` — verify `RepoConfig` class no longer exists

**`tests/test_cli.py`:**
- `test_register_detects_remote` — `org/repo` format is detected vs local path
- `test_register_local_path_still_works` — existing behavior preserved

**`tests/test_session.py`** (new):
- `test_cleanup_worktree_removes_branch` — mock subprocess, verify git commands
- `test_cleanup_worktree_kills_session_first` — verify tmux kill before git cleanup
- `test_sync_main_branch_fetches_and_resets` — mock subprocess, verify commands

### Level 2 — Integration (manual, not in CI)

- Register a real repo via `modastack register org/repo --linear-project KEY` on a test machine
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

### Step 1: Config consolidation (`modastack/config.py`)
- Add `RepoEntry` dataclass with `path`, `remote`, `linear_project`, `credentials`, `trigger_labels`, `skip_labels`
- Move `get_credentials()` method onto `RepoEntry`
- Update `GlobalConfig.repos` type to `list[RepoEntry]`
- Update `load()` parsing — dict entries only
- Update `save()` serialization
- Add `repo_paths` convenience property
- Add `get_repo(path)` lookup helper
- Delete `RepoConfig` class entirely

### Step 2: Migrate consumers from `RepoConfig` to `RepoEntry`
- `manager/events/pollers.py`: replace `RepoConfig.from_file()` with `config.get_repo()`
- `manager/events/webhook_server.py`: same replacement
- `modastack/scanner.py`: change signature to accept `linear_project: str` instead of `RepoConfig`
- Remove all `from modastack.config import RepoConfig` imports

### Step 3: Simplify `modastack/setup.py`
- Remove `generate_dispatch_yaml()` and `setup_repo()` (no more `.modastack.yaml` generation)
- Keep detection functions (`detect_test_command`, `detect_linear_project`, `detect_skills`, `detect_package_manager`) — used at register-time and by engineer skills
- Add `install_skill_symlinks(repo_path)` function (extracted from CLI)
- Add `full_setup(repo_path)` that installs skills + bootstraps Linear board

### Step 4: CLI `register` accepts org/repo (`modastack/cli.py`)
- Change `register` to accept `target` string (not `Path(exists=True)`)
- Add `--linear-project` and `--credentials` options
- Add `org/repo` detection via regex
- Clone via `gh repo clone`, auto-detect settings, call `full_setup()`, register in config
- Add tests

### Step 5: Auto-clone on startup (`manager/events/consumer.py`)
- Add `_ensure_repos()` function calling `install_skill_symlinks()` after clone
- Call it at top of `run()`

### Step 6: Main branch sync (`modastack/session.py`)
- Add `sync_main_branch()` using `git symbolic-ref refs/remotes/origin/HEAD`
- Call from `spawn_session()` before creating the worktree

### Step 7: Worktree cleanup (`modastack/session.py`)
- Add `cleanup_worktree()` function

### Step 8: Git identity on init (`modastack/cli.py`)
- Add `--git-name` and `--git-email` options to `init`
- Set git config when not already configured

### Step 9: Manager prompt + deploy script
- Add repo setup instructions to `manager/prompt.md`
- Simplify `deploy/setup-ec2.sh` post-install steps

### Step 10: Delete stale artifacts
- Delete `example.modastack.yaml`
- Remove `.modastack.yaml` references from CLAUDE.md, README.md, skill docs
- Update tests that referenced `RepoConfig` or `.modastack.yaml`

### Step 11: Tests

**`tests/test_config.py`:**
- `test_repo_entry_from_dict` — dict -> RepoEntry with all fields
- `test_repo_entry_defaults` — missing optional fields get defaults
- `test_global_config_repos_roundtrip` — save + load preserves all fields
- `test_repo_paths_property` — returns list[Path]
- `test_get_repo_found` — lookup by path works
- `test_get_repo_not_found` — returns None for unknown path

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

## What already exists

- `modastack setup` handles the full setup flow — we simplify it (no more `.modastack.yaml` gen)
- `kill_session()` already handles tmux cleanup — `cleanup_worktree()` extends it with git cleanup
- `GlobalConfig.load()/save()` already round-trips YAML — we extend the repo entries
- Detection functions in `setup.py` (`detect_test_command`, `detect_linear_project`, `detect_skills`) — kept, used at register-time

## Failure modes

| Codepath | Failure scenario | Test? | Error handling? | User visible? |
|---|---|---|---|---|
| `_ensure_repos` clone | `gh` not authed, repo private | Yes | Logs error, skips repo | Log only (no Slack) |
| `sync_main_branch` fetch | Network down, remote unreachable | Yes | Logs warning, returns False, spawn continues | No — engineer starts with stale main |
| `sync_main_branch` reset | Merge conflict (shouldn't happen on remote) | Yes | Logs warning, returns False | No |
| `cleanup_worktree` remove | Worktree has locked files | Yes | `--force` flag handles most cases, logs warning | No |
| `register` clone | Invalid org/repo format, 404 | Yes | Error message printed | CLI output |
| `full_setup` skills install | modastack repo moved, symlinks broken | No | Skills dir check exists | CLI output |
