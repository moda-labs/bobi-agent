# MDS-21: Stall Detection and Auto-Routing for Engineer Sessions

## Problem

Engineer sessions can silently stall in four ways, all invisible to the manager:

1. **Stalled output** — session finishes a phase and idles at the prompt. The worker poller sees `waiting_input` but has no concept of how long it's been idle. BET-11 was stuck for ~55 min before manual intervention.

2. **Permission prompt blocked** — session hit an unexpected permission prompt and is waiting for interactive input that will never come. Session appears alive but is permanently blocked.

3. **Silent crash** — tmux session exists but the claude process inside it died. `tmux has-session` returns true, the poller reports it as `working`, but nothing is running.

4. **Extended no-progress** — session is technically alive but producing no meaningful output for 10+ minutes. May be stuck in a loop, waiting on a rate limit, or genuinely thinking — but past a threshold it needs intervention.

## Design

### Overview

Add a heartbeat tracking layer to `_poll_workers` in `pollers.py` that detects stalls by hashing pane output over time. Emit new event types that the manager can act on. Also add process liveness checks to catch silent crashes early.

### Heartbeat via output hashing

**Where:** `manager/events/pollers.py` — `_poll_workers`

Currently, `_poll_workers` captures the last 5 lines of pane content, determines state (`working` or `waiting_input`), and emits `worker.<state>` on state changes. It has no concept of time or progress.

**Change:** Track a hash of the full captured pane content per session, with timestamps.

```python
# New module-level state in _poll_workers
heartbeats: dict[str, dict] = {}
# Per session: {
#   "hash": str,           # hash of last captured pane content
#   "last_change": float,  # time.monotonic() when hash last changed
#   "alerted_stall": bool, # already emitted worker.stalled
#   "alerted_stuck": bool, # already emitted worker.stuck
# }
```

Each poll cycle (every 5s):
1. Capture pane content (already done — extend from `-S -5` to `-S -30` for better hash stability)
2. Compute `hashlib.md5(pane_content.encode()).hexdigest()`
3. If hash changed → update `last_change`, reset alert flags
4. If hash unchanged and current state is NOT `waiting_input`:
   - Unchanged > `STALL_THRESHOLD` (5 min) and not yet alerted → emit `worker.stalled`
   - Unchanged > `STUCK_THRESHOLD` (10 min) and not yet alerted → emit `worker.stuck`

**Thresholds** (configurable module constants):
```python
STALL_THRESHOLD_SECS = 300   # 5 min — emit worker.stalled
STUCK_THRESHOLD_SECS = 600   # 10 min — emit worker.stuck
```

**Why exclude `waiting_input`:** A session at the idle prompt is not stalled — it's waiting for the manager to inject the next skill. The manager handles this separately (see auto-routing below). Including it would generate false positives every time a phase completes normally.

### Permission prompt detection

**Where:** `manager/events/pollers.py` — `_poll_workers`, and `modastack/session.py` — `detect_state`

`session.py:detect_state` already detects `asking_question` by looking for numbered option patterns. Permission prompts are different — they look like:

```
  Allow Read for /path/to/file? (y/n)
```

Or the tool-approval UI with options like "Yes, allow once" / "Yes, allow all".

**Change:** Add a `permission_blocked` state to `detect_state`:

```python
# After the asking_question check, before the waiting_input check:
permission_patterns = [
    r"Allow .+ \(y/n\)",
    r"Do you want to proceed",
    r"Yes, allow once",
    r"Allow all",
]
for line in last_lines:
    if any(re.search(p, line) for p in permission_patterns):
        return {"state": "permission_blocked", "prompt_line": line.strip()}
```

In `_poll_workers`, when `detect_state` returns `permission_blocked`:
- Emit `worker.permission_blocked` with the prompt line in the event data
- The manager decides: auto-approve (send `y` or navigate to "Allow all") or kill and respawn with `--dangerously-skip-permissions`

**Why detect_state and not just pollers?** `detect_state` is the canonical state detection function used by both pollers and the manager directly. Putting the pattern matching there keeps detection consistent.

### Process liveness check

**Where:** `manager/events/pollers.py` — `_poll_workers`

`session.py:detect_state` already does a process liveness check (pane_pid + pgrep), but `_poll_workers` doesn't use `detect_state` — it does its own lightweight pane capture. This means silent crashes are invisible to the poller.

**Change:** Use `detect_state` from `session.py` in the poller for richer state detection:

```python
from modastack.session import detect_state as detect_session_state

# Inside the for session_name loop:
iid = ...  # existing derivation
state_info = detect_session_state(iid)
sess_state = state_info["state"]
```

This replaces the poller's inline pane parsing with the canonical `detect_state`, which already handles:
- `waiting_input` (prompt detection)
- `working` (default)
- `asking_question` (numbered options)
- `exited` (session gone or process dead) ← catches silent crashes
- `permission_blocked` (new, see above)

When `detect_state` returns `exited` but `tmux has-session` is true → the process died inside the session. Emit `worker.process_dead`:

```python
if sess_state == "exited" and tmux_session_exists:
    bus.push("worker.process_dead", "worker", {
        "issue_id": iid,
        "session_name": session_name,
        "reason": "tmux session exists but claude process is not running",
    })
```

### Auto-routing after idle

**Where:** `manager/prompt.md` (manager instructions)

Currently when a session finishes a phase, it emits `worker.waiting_input`. The manager sees this but doesn't always act — it may not realize the session is idle because it finished a phase vs. idle because it's waiting for human input.

**Change:** Add phase-to-next-skill routing instructions to `manager/prompt.md`:

```markdown
## Auto-routing stalled engineers

When you see `worker.waiting_input` for a session, check its handoff file
(`<worktree>/.modastack/handoff.md`). If the phase has a clear next step,
inject the next skill immediately:

| Handoff phase       | Next action                          |
|---------------------|--------------------------------------|
| triage_complete     | inject `/spec`                       |
| spec_complete       | inject `/implement`                  |
| implement_complete  | inject `/prepare-pr`                 |
| review_complete     | inject `/feedback` (if comments)     |

Don't wait for a separate event. If the handoff says the phase is done,
route immediately.
```

This doesn't require code changes — it's a prompt update. The manager already has the tools to read files and inject into tmux sessions.

### New event types

All events flow through `bus.push()` which already supports arbitrary event types. No changes to `bus.py`.

| Event | Trigger | Data fields |
|-------|---------|-------------|
| `worker.stalled` | Output hash unchanged > 5 min, state != `waiting_input` | `issue_id`, `session_name`, `idle_seconds`, `last_output_snippet` |
| `worker.stuck` | Output hash unchanged > 10 min | `issue_id`, `session_name`, `idle_seconds`, `last_output_snippet` |
| `worker.permission_blocked` | Permission prompt detected in pane | `issue_id`, `session_name`, `prompt_line` |
| `worker.process_dead` | tmux session exists but no claude child process | `issue_id`, `session_name`, `reason` |

### Manager response instructions

Add to `manager/prompt.md`:

```markdown
## Handling stall events

| Event                      | Response                                           |
|----------------------------|----------------------------------------------------|
| `worker.stalled` (5 min)   | Check handoff for next step. If found, inject it.  |
|                            | If no handoff or unclear, send Enter to nudge.      |
|                            | Post to Slack: "{issue} engineer idle for 5 min"   |
| `worker.stuck` (10 min)    | Kill session. Post to Slack with context.           |
|                            | If work is incomplete, respawn with /pickup.        |
| `worker.permission_blocked`| Kill session, respawn with --dangerously-skip-permissions. |
|                            | Post to Slack: "{issue} was permission-blocked"    |
| `worker.process_dead`      | Clean up tmux session. Check handoff for state.    |
|                            | If work incomplete, respawn. Post to Slack.        |
```

## Files to change

| File | Change |
|------|--------|
| `manager/events/pollers.py` | Add heartbeat tracking (hash + timestamps), use `detect_state` from session.py, emit new events |
| `modastack/session.py` | Add `permission_blocked` state detection in `detect_state` |
| `manager/prompt.md` | Add stall event handling instructions and auto-routing table |
| `tests/test_stall_detection.py` | Unit tests for heartbeat logic, permission detection, process liveness |

## Files NOT changed

| File | Why |
|------|-----|
| `manager/events/bus.py` | Already supports arbitrary event types — no changes needed |
| `manager/events/consumer.py` | Already writes all event types to the pending file — no changes needed |
| `manager/events/webhook_server.py` | Stall detection is polling-only — not relevant |

## Test plan

1. **Heartbeat tracking:** Mock `subprocess.run` to return static pane content, advance time past thresholds, verify `worker.stalled` and `worker.stuck` events are emitted with correct data.

2. **Permission detection:** Feed `detect_state` pane content containing permission prompts, verify it returns `permission_blocked` with the prompt line.

3. **Process liveness:** Mock `pgrep` to return no children for a session with a live tmux session, verify `worker.process_dead` is emitted.

4. **Alert dedup:** Verify that stall/stuck events are emitted only once per stall episode (reset on hash change).

5. **No false positives on waiting_input:** Verify that sessions in `waiting_input` state never emit `worker.stalled` regardless of idle time.

## Risks and mitigations

| Risk | Mitigation |
|------|-----------|
| Hash instability from clock/cursor blink in pane | Capture 30 lines (not 5) for more stable hashing. Strip ANSI codes before hashing. |
| False stall during legitimate long thinking | 5 min threshold is generous. `worker.stalled` triggers a nudge, not a kill. Only `worker.stuck` at 10 min kills. |
| Manager respawn loops (kill → respawn → stall → kill) | Track respawn count per issue in manager memory. After 2 respawns, escalate to human instead of respawning. |
| `detect_state` regression from permission patterns | Permission patterns are checked after `asking_question`, so numbered-option prompts still match first. Tests cover both. |

## Complexity

**Medium.** The core change is ~80 lines in `pollers.py`, ~15 lines in `session.py`, and prompt updates in `manager/prompt.md`. No new dependencies, no schema changes, no new files beyond the test file.
