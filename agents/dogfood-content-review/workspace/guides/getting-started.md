# Getting Started with Modastack
> Set up modastack to manage AI agents in your repo.

## Prerequisites

- Python 3.11+
- `uv` package manager (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- A GitHub repo with issues enabled
- Claude CLI installed

## Install

```bash
uv tool install modastack
```

## Setup

1. Navigate to your repo:
   ```bash
   cd ~/dev/your-repo
   ```

2. Install an agent team (from the directory that will own the
   installation — this creates the single `.modastack/` with
   `agent.yaml` inside):
   ```bash
   modastack install eng-team
   ```

3. Start the manager:
   ```bash
   modastack start
   ```

4. Verify it's running:
   ```bash
   modastack status
   ```

## First task

1. Create a GitHub issue in your repo with the `agent` label
2. The manager picks it up and runs the appropriate workflow
3. Watch progress with `modastack log manager`

## Troubleshooting

- **"no Modastack installation found"**: run from inside the installation tree — the directory where `modastack install` created `.modastack/agent.yaml`
- **Manager not responding**: Check `modastack doctor` for diagnostics
- **No events arriving**: Verify event server with `modastack event-server status`
