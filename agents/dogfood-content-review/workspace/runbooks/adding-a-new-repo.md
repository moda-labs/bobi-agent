# Adding a New Repo to Bobi
> How to set up bobi in a new repository.

## Trigger

A new repo needs AI agent management.

## Prerequisites

- Bobi installed (`uv tool install bobi`)
- Write access to the repo
- GitHub webhook access (for event delivery)

## Steps

1. **Choose the named Bobi Agent**
   ```bash
   bobi agents list
   ```

2. **Check the installation**
   New repos don't get their own cwd-scoped setup. They are managed by a
   named Bobi Agent under `BOBI_HOME`; keep the repo checkout clean and point
   the agent's workflows or tasks at the repo path explicitly.

3. **Configure task tracking**
   Edit the Bobi Agent source and reinstall so `run/package/agent.yaml` is
   regenerated:
   ```yaml
   task_tracking:
     system: github-issues  # or "linear"
     trigger_labels:
     - agent
   ```

4. **Set up event delivery**
   ```bash
   bobi agent <name> event-server start
   ```
   Then add the webhook URL to your GitHub repo settings:
   - URL: `http://localhost:8080/webhooks/github`
   - Content type: `application/json`
   - Events: Issues, Pull requests, Push, Check runs

5. **Start the manager**
   ```bash
   bobi agent <name> start
   ```

6. **Verify with doctor**
   ```bash
   bobi agent <name> doctor
   ```
   All checks should pass.

## Custom roles and workflows

If the default engineering workflows don't fit, create custom ones:
- `roles/<role>/ROLE.md` in the source package — agent role prompts
- `run/package/workflows/<name>.yaml` — custom workflow definitions

See the bobi docs for the workflow YAML reference.

Last verified: 2026-06-04
