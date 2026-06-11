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

2. Initialize modastack:
   ```bash
   modastack init
   ```
   This creates `.modastack/config.yaml` and `.modastack/local.yaml`.

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

- **"Not inside a modastack repo"**: Make sure `.modastack/config.yaml` exists
- **Manager not responding**: Check `modastack doctor` for diagnostics
- **No events arriving**: Verify event server with `modastack event-server status`
