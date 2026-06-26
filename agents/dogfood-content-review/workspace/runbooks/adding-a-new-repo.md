# Adding a New Repo to Bobi
> How to set up bobi in a new repository.

## Trigger

A new repo needs AI agent management.

## Prerequisites

- Bobi installed (`uv tool install bobi`)
- Write access to the repo
- GitHub webhook access (for event delivery)

## Steps

1. **Navigate to the repo**
   ```bash
   cd ~/dev/new-repo
   ```

2. **Check the installation**
   New repos don't get their own bobi setup — there is exactly one
   `.bobi/` per installation, at the root where `bobi install`
   was run. Clone the repo under that root and keep the checkout clean
   (no `.bobi/` inside it).

3. **Configure task tracking**
   Edit `.bobi/agent.yaml` at the installation root:
   ```yaml
   task_tracking:
     system: github-issues  # or "linear"
     trigger_labels:
     - agent
   ```

4. **Set up event delivery**
   ```bash
   bobi event-server start
   ```
   Then add the webhook URL to your GitHub repo settings:
   - URL: `http://localhost:8080/webhooks/github`
   - Content type: `application/json`
   - Events: Issues, Pull requests, Push, Check runs

5. **Start the manager**
   ```bash
   bobi start
   ```

6. **Verify with doctor**
   ```bash
   bobi doctor
   ```
   All checks should pass.

## Custom roles and workflows

If the default engineering workflows don't fit, create custom ones:
- `.bobi/agents/<role>.md` — agent role prompts
- `.bobi/workflows/<name>.yaml` — custom workflow definitions

See the bobi docs for the workflow YAML reference.

Last verified: 2026-06-04
