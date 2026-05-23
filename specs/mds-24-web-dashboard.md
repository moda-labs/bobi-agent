# MDS-24: Web dashboard — task status and event log viewer

## Classification

**New feature** — monitoring/visibility dashboard for the modastack event loop.

## Problem

Operators have no visual overview of what modastack is doing. The CLI
commands (`modastack status`, `modastack events`, `modastack decisions`)
provide raw text output that requires terminal access and manual polling.
There's no way to watch events arrive in real time, filter by source, or
see at a glance which engineer sessions are active vs idle.

## Scope

A local web dashboard at `http://localhost:8095` showing:

1. **System status bar** — manager alive/dead, uptime, event bus depth
2. **Active sessions** — engineer tmux sessions with live state (working,
   waiting_input, asking_question, exited)
3. **Event log** — filterable, paginated table of events from `events.jsonl`
4. **Decisions log** — recent manager decision batches with reasoning

Out of scope: authentication (local tool), database migration, modifying
the event bus or consumer, mobile layout, editing/mutating state from
the UI.

## Data sources

All reads — no writes. The dashboard is a view layer over existing state.

| Data | Source | Method |
|---|---|---|
| Events | `~/.modastack/manager/events.jsonl` | Read JSONL, parse JSON per line |
| Decisions | `~/.modastack/manager/decisions.jsonl` | Read JSONL, parse JSON per line |
| Engineer sessions | `tmux list-sessions` + `capture-pane` | Subprocess, same as `modastack status` |
| Manager state | `tmux` session `moda-manager` | Subprocess, same as `modastack tick` |
| Config | `~/.modastack/config.yaml` + repo `.modastack.yaml` | YAML parse via existing `config.py` |

### Event shape (from `bus.py`)

```json
{
  "type": "slack.dm | task.opened | github.pr.opened | worker.waiting_input | ...",
  "source": "slack | github | github-issues | linear | worker | system",
  "timestamp": "2025-01-15T14:30:00",
  "data": { "issue_id": "...", "title": "...", "text": "...", ... }
}
```

### Decision shape (from `consumer.py`)

```json
{
  "timestamp": "2025-01-15 14:30:00",
  "events": 3,
  "event_types": ["slack.dm", "worker.waiting_input"]
}
```

## Technical approach

### New dependency: FastAPI + uvicorn

FastAPI is the right fit: async, built-in JSON serialization, Jinja2
templating support, and lightweight. The existing webhook server uses
`http.server` which can't serve templates or handle the routing needed.

```toml
# Added to pyproject.toml dependencies
"fastapi>=0.115",
"uvicorn[standard]>=0.30",
"jinja2>=3.1",
```

### Module structure

```
dashboard/
├── __init__.py
├── app.py          # FastAPI app, routes, startup
├── data.py         # Read events.jsonl, decisions.jsonl, tmux state
└── templates/
    └── index.html  # Single-page dashboard (HTML + inline JS/CSS)
```

### API endpoints

```
GET /                     → HTML dashboard page
GET /api/status           → { manager: {alive, state}, sessions: [...], bus_pending: int }
GET /api/events?limit=50&offset=0&source=&type=  → { events: [...], total: int }
GET /api/decisions?limit=10  → { decisions: [...] }
GET /api/events/stream    → SSE stream of new events (optional, phase 2)
```

### CLI command

```bash
modastack dashboard              # start dashboard on :8095
modastack dashboard --port 9000  # custom port
```

Added to `cli.py` as a new Click command:

```python
@main.command()
@click.option("--port", default=8095, help="Dashboard port")
def dashboard(port):
    """Start the web dashboard."""
    from dashboard.app import run_dashboard
    run_dashboard(port=port)
```

### Dashboard page

Single HTML file with vanilla JS (no build step, no npm). Layout:

```
┌─────────────────────────────────────────────────────┐
│  modastack                          Manager: ● live  │
├──────────────────────┬──────────────────────────────┤
│  Active Sessions     │  Event Log                    │
│                      │                               │
│  ● moda-eng-42       │  14:30:02  slack    slack.dm  │
│    working           │    "what's the status of..."  │
│                      │  14:29:55  worker   worker... │
│  ● moda-eng-38       │    issue_id: ENG-38           │
│    waiting_input     │  14:29:30  github   github... │
│                      │    PR #12 opened              │
│                      │                               │
│                      │  [source ▾] [type ▾] [older]  │
├──────────────────────┴──────────────────────────────┤
│  Recent Decisions                                    │
│  14:28  slack.dm, worker.waiting_input  (2 events)  │
│  14:25  task.opened                     (1 event)   │
└─────────────────────────────────────────────────────┘
```

Auto-refresh: JS polls `/api/status` and `/api/events` every 5 seconds.
No WebSocket needed for v1 — polling matches the system's own batch window.

### Data layer (`data.py`)

```python
def read_events(limit=50, offset=0, source=None, type_filter=None) -> tuple[list[dict], int]:
    """Read events.jsonl in reverse chronological order with filtering."""

def read_decisions(limit=10) -> list[dict]:
    """Read decisions.jsonl, most recent first."""

def get_sessions() -> list[dict]:
    """List tmux sessions with state detection (reuses modastack/session.py logic)."""

def get_manager_status() -> dict:
    """Manager alive/dead + state."""
```

For large JSONL files: read from the end of the file using `seek` to
avoid loading the entire file. Cache the file size and re-read only
when it changes.

## Size verdict

**Medium** — new module with 4 files, new dependency, new CLI command.
No changes to existing modules beyond `cli.py` (one new command) and
`pyproject.toml` (three new deps).

## Files changed

| File | Change |
|---|---|
| `pyproject.toml` | Add fastapi, uvicorn, jinja2 deps |
| `modastack/cli.py` | Add `dashboard` command |
| `dashboard/__init__.py` | New — empty |
| `dashboard/app.py` | New — FastAPI app + routes |
| `dashboard/data.py` | New — data access layer |
| `dashboard/templates/index.html` | New — dashboard UI |

## Verification plan

**Level 1 (Automated):**
- `pytest tests/` passes (no regressions)
- New tests: `tests/test_dashboard.py`
  - `test_read_events_empty` — no JSONL file
  - `test_read_events_with_data` — parse sample events
  - `test_read_events_filtering` — source/type filters
  - `test_status_endpoint` — FastAPI test client, mock tmux
  - `test_events_endpoint_pagination` — limit/offset

**Level 2 (Type check):** N/A — project doesn't use mypy currently.

**Level 3 (Manual QA):**
1. `modastack dashboard` starts without errors
2. Dashboard loads at `http://localhost:8095`
3. Sessions panel shows tmux sessions (or "No active sessions")
4. Event log shows entries from `events.jsonl` (or "No events yet")
5. Filters narrow results by source and type
6. Auto-refresh picks up new events within 5 seconds
7. Manager status indicator reflects actual tmux state

## Implementation plan

1. Add FastAPI + uvicorn + jinja2 to `pyproject.toml`
2. Create `dashboard/data.py` — read events, decisions, session state
3. Create `dashboard/app.py` — FastAPI app with API routes + template
4. Create `dashboard/templates/index.html` — single-page dashboard
5. Add `dashboard` command to `modastack/cli.py`
6. Add `dashboard` package to hatch build targets
7. Write `tests/test_dashboard.py`
8. Manual smoke test

Estimated complexity: **medium**
