# Getting Started with Bobi
> Set up bobi to manage AI agents in your repo.

## Prerequisites

- Python 3.11+
- `uv` package manager (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- A GitHub repo with issues enabled
- Claude CLI installed

## Install

```bash
uv tool install bobi
```

## Setup

1. Install an agent team into a named machine-wide slot:
   ```bash
   bobi agents install eng-team --name eng
   ```

2. Start the manager:
   ```bash
   bobi agent eng start
   ```

3. Verify it's running:
   ```bash
   bobi agent eng status
   ```

## First task

1. Create a GitHub issue in your repo with the `agent` label
2. The manager picks it up and runs the appropriate workflow
3. Watch progress with `bobi agent eng transcript show manager`

## Troubleshooting

- **"No Bobi Agent runtime selected"**: use a named command such as `bobi agent eng status`
- **Manager not responding**: check `bobi agent eng doctor` for diagnostics
- **No events arriving**: verify the event server with `bobi agent eng event-server status`
