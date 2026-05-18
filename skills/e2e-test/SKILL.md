# /e2e-test — Run the agentd end-to-end integration test

Run the full agentd integration test suite. This creates real Linear issues,
spawns real Claude Code sessions in tmux, and verifies the entire dispatch
pipeline works end-to-end.

## Prerequisites

Before running, verify:

1. **tmux installed**: `tmux -V`
2. **claude CLI authenticated**: `claude --version` (must be logged in with Max)
3. **Linear API key configured**: check `~/.dispatch/credentials.yaml` has the `agentd` entry
4. **agentd installed**: `dispatch --help`
5. **No stale tmux sessions**: `tmux ls` — kill any `agentd-*` sessions
6. **No stale state**: `rm -f ~/.dispatch/state.json`

## Test tiers

### Tier 1: Unit tests (free, <3s)

```bash
source .venv/bin/activate
pytest tests/ --ignore=tests/integration/ -v
```

Tests: config loading, state store, scanner classification, setup detection,
skill installation, summarizer phase detection. No network, no Claude, no Linear.

### Tier 2: tmux + Claude integration (uses Max credits, ~50s)

```bash
source .venv/bin/activate
pytest tests/integration/test_tmux_claude.py -v -s --timeout=120
```

Tests the tmux puppeting layer:
- Spawn interactive Claude Code in tmux
- Detect ready state (waiting_input)
- Inject tasks, verify responses
- Multi-turn context preservation
- AskUserQuestion detection and answering
- Session lifecycle (kill, detect exited)

Each test spawns a fresh session and cleans up after.

### Tier 3: Full dispatch loop (uses Max credits + Linear API, ~5-10min)

```bash
source .venv/bin/activate
pytest tests/integration/test_full_loop.py -v -s --timeout=600
```

Tests the complete lifecycle:
- Creates a test issue in Linear with the `agent` label
- Runs `dispatch cycle` to pick it up
- Verifies the issue moves through states (Todo → In Progress → In Review)
- Cleans up the test issue after

**Warning**: This creates real Linear issues in the AGD project. Issues are
cleaned up automatically, but check Linear if a test fails mid-run.

## Running a manual E2E test

For a hands-on test of the full flow:

### Step 1: Clean slate

```bash
tmux kill-server 2>/dev/null            # kill all tmux sessions
rm -f ~/.dispatch/state.json            # clear state
git worktree list | grep worktrees | awk '{print $1}' | xargs -I{} git worktree remove --force {}
```

### Step 2: Create a test issue

Create an issue in Linear:
- Team: AGD
- Title: "Test: add a comment to README.md with the current date"
- Label: `agent`
- State: Todo

Or create via API:

```bash
source .venv/bin/activate
python3 -c "
import asyncio, truststore, httpx, time
truststore.inject_into_ssl()
from pathlib import Path; import yaml
creds = yaml.safe_load((Path.home() / '.dispatch' / 'credentials.yaml').read_text())
api_key = creds.get('agentd', {}).get('linear_api_key', '')
async def create():
    async with httpx.AsyncClient() as c:
        r = await c.post('https://api.linear.app/graphql',
            headers={'Authorization': api_key, 'Content-Type': 'application/json'},
            json={'query': '{teams(filter:{key:{eq:\"AGD\"}}){nodes{id states{nodes{id name type}}}}}'})
        team = r.json()['data']['teams']['nodes'][0]
        team_id = team['id']
        todo_id = next(s['id'] for s in team['states']['nodes'] if s['name'] == 'Todo')

        r2 = await c.post('https://api.linear.app/graphql',
            headers={'Authorization': api_key, 'Content-Type': 'application/json'},
            json={'query': '{issueLabels(filter:{name:{eq:\"agent\"}}){nodes{id}}}'})
        label_id = r2.json()['data']['issueLabels']['nodes'][0]['id']

        r3 = await c.post('https://api.linear.app/graphql',
            headers={'Authorization': api_key, 'Content-Type': 'application/json'},
            json={
                'query': 'mutation(\$t: String!, \$title: String!, \$d: String!, \$l: [String!], \$s: String!) { issueCreate(input: { teamId: \$t, title: \$title, description: \$d, labelIds: \$l, stateId: \$s }) { success issue { identifier } } }',
                'variables': {
                    't': team_id, 'title': f'Test: add date comment to README ({int(time.time())})',
                    'd': 'Add a comment at the bottom of README.md with the current UTC date.',
                    'l': [label_id], 's': todo_id,
                }
            })
        print(r3.json()['data']['issueCreate']['issue']['identifier'])
asyncio.run(create())
"
```

### Step 3: Run one dispatch cycle

```bash
dispatch cycle
```

Expected: dispatched=1. A tmux session `agentd-<issue>` is created.

### Step 4: Observe the agent

```bash
tmux attach -t agentd-<issue>     # watch it work live
# Ctrl-B D to detach
dispatch status                    # check state
dispatch watch                     # live dashboard
```

### Step 5: Run subsequent cycles

Each cycle the daemon:
1. Detects the agent is idle
2. Summarizer inspects the worktree, writes the handoff
3. Routes to the next skill in the same tmux session

```bash
dispatch cycle    # summarize + route to /implement
dispatch cycle    # summarize + route to /ship-pr
```

### Step 6: Verify on Linear

Check the issue moved through: Todo → In Progress → In Review.
Review the PR on GitHub. Merge it.

```bash
dispatch cycle    # detect merge → move to Done
```

### Step 7: Clean up

```bash
tmux kill-session -t agentd-<issue>
git worktree remove --force worktrees/<issue>
```

## What to watch for

- **Agent stuck at prompt**: run `dispatch cycle` — the summarizer will
  inspect and route the next phase
- **"Unknown command" errors**: skills not installed. Check `.claude/skills/`
  has symlinks to `skills/`
- **Stale worktree**: if a previous test left a worktree, the agent may
  find existing code. Clean up with `git worktree remove --force`
- **Stale branch**: `git branch -D agent/<issue>` if the branch exists
  from a prior run
- **tmux pane too small**: the daemon spawns 200x50 terminals. If capture
  looks wrong, check `tmux list-windows -t <session> -F '#{window_width}x#{window_height}'`
