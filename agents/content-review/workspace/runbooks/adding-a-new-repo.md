# Adding a New Repo to Modastack
> How to set up modastack in a new repository.

## Trigger

A new repo needs AI agent management.

## Prerequisites

- Modastack installed (`uv tool install modastack`)
- Write access to the repo
- GitHub webhook access (for event delivery)

## Steps

1. **Navigate to the repo**
   ```bash
   cd ~/dev/new-repo
   ```

2. **Initialize modastack**
   ```bash
   modastack init
   ```
   This creates:
   - `.modastack/config.yaml` — shared team config (check this in)
   - `.modastack/local.yaml` — your operator credentials (gitignored)

3. **Configure task tracking**
   Edit `.modastack/config.yaml`:
   ```yaml
   task_tracking:
     system: github-issues  # or "linear"
     trigger_labels:
     - agent
   ```

4. **Set up event delivery**
   ```bash
   modastack event-server start
   ```
   Then add the webhook URL to your GitHub repo settings:
   - URL: `http://localhost:8080/webhooks/github`
   - Content type: `application/json`
   - Events: Issues, Pull requests, Push, Check runs

5. **Start the manager**
   ```bash
   modastack start
   ```

6. **Verify with doctor**
   ```bash
   modastack doctor
   ```
   All checks should pass.

## Custom roles and workflows

If the default engineering workflows don't fit, create custom ones:
- `.modastack/agents/<role>.md` — agent role prompts
- `.modastack/workflows/<name>.yaml` — custom workflow definitions

See the modastack docs for the workflow YAML reference.

Last verified: 2026-06-04
