# Spec: Move agent worktrees into modastack repo

**Issue:** moda-labs/modastack#85
**Status:** Draft
**Author:** agent

## Problem

Agent worktrees are currently created inside each target project repo at
`<repo>/worktrees/<issue-id>/`. This causes three problems:

1. **Git pollution** — `worktrees/` must be gitignored in every registered repo.
   Agents that forget or fail to gitignore it leak large directories into
   `git status`.

2. **Skill installation overhead** — `modastack setup` symlinks ~15 skills into
   `<repo>/.claude/skills/` so that agents can use `/pickup`, `/implement`, etc.
   These symlinks point back to the modastack repo via relative paths, which
   break if the repo moves. Every new repo requires this ceremony.

3. **Fragile symlinks** — the relative symlinks from `<repo>/.claude/skills/foo`
   → `../../../modastack/roles/engineer/process/foo` are brittle and create a
   hidden coupling between repo location and modastack install path.

## Proposal

Create worktrees physically inside the modastack repo directory tree. Claude
Code resolves skills by walking up the directory tree from the working
directory. If the worktree lives under `~/dev/modastack/`, it automatically
inherits all skills defined in `~/dev/modastack/.claude/skills/` — no symlinks
needed in the target repo.

## Design

### 1. Worktree location

**Current:**
```
<repo>/worktrees/<issue-id>/          # e.g. ~/dev/memorize/worktrees/mem-42/
```

**New:**
```
~/dev/modastack/worktrees/<repo-name>/<issue-id>/
# e.g. ~/dev/modastack/worktrees/memorize/mem-42/
```

The `<repo-name>` segment is derived from the repo's directory name (the last
component of its registered path). This keeps worktrees from different repos
separated and avoids issue-id collisions across projects.

The `worktrees/` directory is already gitignored in the modastack repo.

### 2. Git worktree creation

A git worktree can point to any path on disk, regardless of where the parent
repo lives. The `git worktree add` command is run from the **target repo** but
specifies an **absolute path** inside modastack:

```bash
# Run from the target repo (e.g. ~/dev/memorize)
cd ~/dev/memorize
git worktree add -b agent/<issue-id> \
    ~/dev/modastack/worktrees/memorize/<issue-id>
```

If the branch already exists:
```bash
git worktree add ~/dev/modastack/worktrees/memorize/<issue-id> agent/<issue-id>
```

The worktree is still a checkout of the **target repo** — only the physical
location changes. Git tracks the worktree via `.git/worktrees/` in the main
repo, so `git worktree list` from `~/dev/memorize` still shows it.

### 3. Skill resolution via directory nesting

Claude Code resolves skills by walking up from `cwd` looking for
`.claude/skills/` directories. With the worktree nested under modastack:

```
~/dev/modastack/                          # has .claude/skills/ with all modastack skills
  worktrees/
    memorize/
      mem-42/                             # agent cwd — checkout of memorize repo
        src/...                           # memorize source code
```

When an agent runs in `~/dev/modastack/worktrees/memorize/mem-42/`, Claude Code
walks up and finds `~/dev/modastack/.claude/skills/`, which contains all
engineer process, practices, and tool skills. No symlinks needed.

Additionally, gstack user-level skills at `~/.claude/skills/` are still found
via the normal home-directory fallback.

### 4. Code changes

#### 4a. `roles/tools/git/SKILL.md` (worktree instructions)

Update the worktree setup section. The agent no longer runs
`git worktree add ... worktrees/<issue-id>` with a relative path. Instead, the
skill instructs the agent to create the worktree at an absolute path under
modastack:

```bash
git worktree add -b agent/<issue-id> \
    ~/dev/modastack/worktrees/<repo-name>/<issue-id>
cd ~/dev/modastack/worktrees/<repo-name>/<issue-id>
```

The `<repo-name>` and modastack path will be provided in the phase context
by the workflow executor.

#### 4b. `modastack/session.py` — `cleanup_worktree()`

**Current** (line 98):
```python
worktree_path = repo_path / "worktrees" / issue_id.lower()
```

**New:**
```python
modastack_root = Path(__file__).parent.parent
repo_name = repo_path.name
worktree_path = modastack_root / "worktrees" / repo_name / issue_id.lower()
```

The `git worktree remove` command must still be run with `cwd=repo_path` (the
target repo) since that's where `.git/worktrees/` lives.

#### 4c. `modastack/subagent.py` — `_build_prompt()` and `run_phase_blocking()`

The prompt builder needs to pass the new worktree base path to the agent so the
`/pickup` skill knows where to create the worktree:

```python
modastack_root = Path(__file__).parent.parent
repo_name = Path(cwd).name
worktree_base = modastack_root / "worktrees" / repo_name
```

Add to the prompt context:
```
Worktree base: ~/dev/modastack/worktrees/<repo-name>/
```

The `cwd` passed to the Claude SDK client for `run_phase_blocking()` should
remain the **target repo** for the initial pickup phase (the agent creates the
worktree and cds into it). For subsequent phases (spec, implement, prepare-pr),
`cwd` should be the **worktree path** so the agent starts in the right place.

#### 4d. `modastack/workflow/actions.py` — `_session_spawn()`

Update to ensure the worktree directory parent exists:

```python
modastack_root = Path(__file__).parent.parent
repo_name = Path(cwd).name
worktree_parent = modastack_root / "worktrees" / repo_name
worktree_parent.mkdir(parents=True, exist_ok=True)
```

Return `worktree_base` in the action output so downstream workflow nodes can
reference it.

#### 4e. `modastack/workflow/executor.py` — `_read_handoff()`

Update the handoff search path. Currently checks:
1. `<repo>/worktrees/<iid>/.modastack/handoff.md`
2. `~/.modastack/handoffs/<iid>.md`

Change path 1 to:
```python
modastack_root / "worktrees" / repo_name / iid / ".modastack" / "handoff.md"
```

#### 4f. Handoff contract — `worktree:` field

The `worktree:` field in handoff YAML will now contain the new path:
```yaml
worktree: /home/ubuntu/dev/modastack/worktrees/memorize/mem-42
```

No structural change to the handoff format — just the path value changes.

### 5. Impact on `modastack setup` / `modastack register`

With worktrees nested under modastack, the following setup steps become
**unnecessary** and can be removed:

| Step | Why it's no longer needed |
|------|--------------------------|
| Symlink skills into `<repo>/.claude/skills/` | Skills are found via directory-tree walking from worktree |
| Add `worktrees/` to `<repo>/.gitignore` | Worktrees no longer live in the target repo |
| Copy hooks into `<repo>/.claude/hooks/` | Hooks can also be resolved from modastack's `.claude/` |

**What remains:**
- Generate `.modastack.yaml` in the target repo (task tracking config)
- Bootstrap task tracker (Linear board / GitHub labels)
- Register repo path in `~/.modastack/config.yaml`
- Add `.modastack/` to `<repo>/.gitignore` (still needed for local state)

The `modastack setup` command shrinks from ~40 lines of skill/hook installation
to just config + tracker setup.

### 6. Migration path

Existing worktrees (in `<repo>/worktrees/`) need to be moved. A migration
command or startup check handles this:

```bash
modastack migrate-worktrees
```

**Algorithm:**
1. For each registered repo in `~/.modastack/config.yaml`:
   a. List worktrees: `git -C <repo> worktree list --porcelain`
   b. For each worktree under `<repo>/worktrees/`:
      - Extract issue-id from path
      - Run `git -C <repo> worktree move <old-path> <new-path>`
      - Update handoff file's `worktree:` field if it exists
2. Clean up stale symlinks in `<repo>/.claude/skills/` (optional, non-breaking)
3. Remove `worktrees/` from `<repo>/.gitignore` (optional cleanup)

`git worktree move` is atomic and preserves branch tracking. No data loss risk.

**Fallback:** If `git worktree move` fails (older git versions), fall back to
remove + re-add:
```bash
git worktree remove <old-path>
git worktree add <new-path> agent/<issue-id>
```

For repos with no active worktrees, no migration is needed — new worktrees will
automatically use the new path.

### 7. Edge cases

**Multiple repos with the same directory name:** Unlikely in practice (e.g. two
repos both named `api/`), but guard against it by using the full `owner/repo`
slug from the git remote as the directory name when a collision is detected.

**Modastack repo itself as a target:** If modastack is registered as a target
repo, worktrees go to `~/dev/modastack/worktrees/modastack/<issue-id>`. This
works fine — git supports nested worktree paths.

**Worktree cleanup on unregister:** When a repo is unregistered, clean up its
worktree directory: `rm -rf ~/dev/modastack/worktrees/<repo-name>/`.

## Non-goals

- Changing the branch naming convention (`agent/<issue-id>`)
- Changing the handoff file format (just the path value)
- Moving handoff files out of `~/.modastack/handoffs/`
- Changing how gstack skills are resolved (they already work via `~/.claude/`)

## Testing

1. **Unit:** Mock `git worktree add` calls in `session.py` and verify paths
2. **Integration:** Register a test repo, run pickup phase, verify worktree
   lands under `modastack/worktrees/<repo>/` and skills resolve correctly
3. **Migration:** Create a worktree at the old path, run migration, verify it
   moves correctly and handoff is updated
