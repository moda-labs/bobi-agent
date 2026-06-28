# Monitors

Monitors are bobi's polling layer. Webhooks tell an agent when something
*happens* (a PR opens, a message arrives); monitors tell it about state that
*drifts* with no event behind it - a PR that went stale, a merge conflict that
appeared, a deploy that started failing, an inbox that filled up. A monitor runs
on a schedule, detects a condition, and publishes it as an event so the manager
reacts to it exactly like any webhook.

This doc covers how the scheduler runs monitors and how the `script_cache`
flavor saves tokens by letting an agent write a check once, then caching the
script it produced.

## Mental model

```
scheduler thread (in the manager process)
  │ every 30s: reload registry, run due monitors
  ▼
detect ──► conditions ──► dedup vs persisted state ──► publish to event server
  │                                                          │
  ├─ notify       (keyed to the due time)                    ▼
  ├─ command      (shell cmd → JSON list)              subscribers, incl.
  ├─ native check (Python runner, e.g. script_cache)   the manager, receive
  └─ description  (short-lived check agent → verdict)   it like any event
```

Every flavor is just a *condition detector*. What happens to detected
conditions is one shared path: dedup, then publish. Nothing is injected
in-process - findings travel through the event server's topic routing, so they
land in `events.jsonl`, get seq/replay durability, and reach every subscriber
identically. See `docs/EVENT_SERVER.md` for the bus itself.

Code lives in `bobi/monitors/`:

| File | Role |
|---|---|
| `schema.py` | `Monitor` dataclass; interval / wall-clock / weekday parsing |
| `registry.py` | Merges monitors from framework defaults + team package + user config |
| `scheduler.py` | Background thread: reload, run due monitors, dedup, publish |
| `*_checks.py` | Native check runners, auto-discovered by glob (`script_cache`, `tool_poll`, …) |
| `curator.py` | The one flavor that writes an artifact instead of returning a verdict |

## Defining a monitor

A monitor is a YAML entry. Reserved keys map to named fields; anything else
lands in `extra` (e.g. `prompt`, `id_field`, `tool`, `query`).

```yaml
- name: stale-prs
  description: PRs with no activity in 2 days
  check: pr_conflicts        # native runner, or use command:/notify:/description:
  interval: 1h               # or at: ["09:00"] with optional tz:/days:
  event: monitor/pr.stale    # defaults to monitor/<name>
  id_field: number           # dedup key within the returned list
```

Configuration merges in tiers, later wins by `name`:

1. **Framework defaults** - `bobi/monitors/framework_defaults.yaml` (shipped).
2. **Team package** - `run/package/monitors.yaml` in the installed image.
3. **User overrides** - the `monitors:` key in `run/package/agent.yaml`.

Set `enabled: false` on a name to switch off a default. The registry reloads
every tick, so monitors added at runtime take effect without a restart.

## Flavors

- **Notification** (`notify: true`) - fires once on every scheduled run, keyed
  to the due time so dedup never suppresses it. For nudges an agent reacts to
  (a twice-daily roundup, a weekly prep-doc), not condition detection.
- **Command** (`command:`) - runs a shell command, parses its JSON output into
  a flat list, and fires `event:` for items with a new `id_field`. Discover the
  right call interactively with `venn tools search/describe/execute` first.
- **Native check** (`check:`) - names a deterministic Python runner shipped with
  the framework or the pack's `monitors/*_checks.py`. `script_cache` is one.
- **Description-only** (`description:`, no command) - when output needs
  interpretation, the scheduler spawns a short-lived check agent that observes
  and returns a verdict. Costs an LLM call per interval; use when diffable JSON
  isn't available.
- **Curator** (`curator: true`) - the one flavor whose agent *writes* an
  artifact (`policy.md`) instead of returning a verdict.

## Scheduling

A monitor runs on an `interval:` (`30s`, `15m`, `1h`, `2d`) or at wall-clock
`at:` times:

| field | meaning |
|---|---|
| `at:` | time(s) of day - `"21:00"` or `["06:00", "18:00"]` |
| `tz:` | IANA timezone for `at:` (e.g. `America/Los_Angeles`); defaults to host local |
| `days:` | weekday(s) the `at:` times may fire - names (`sun`) or numbers (`0`/`7`=Sun … `6`=Sat). Absent ⇒ every day |

`days:` is how you express **weekly** recurrence - a filter on which weekdays an
`at:` time is eligible. An at-monitor never fires on first sight (the first tick
records a baseline). A plain daily `at:` slot missed while the manager was down
fires **once, late**, on the next tick (catch-up); a weekly (`days:`-gated) slot
does **not** catch up - a missed run is skipped. DST is handled.

## The detect → publish path

Each tick, the scheduler runs every due monitor and feeds its result through one
reconcile path (`scheduler.py`):

- A detector returns a **list of conditions**, or **`None`** when the detection
  itself failed (command error, check exception, no verdict).
- `None` is *indeterminate*: state is left untouched, nothing fires, the next
  interval retries. An **empty list** means "all clear" and clears active
  conditions.
- Conditions are deduped by `id_field` (a SHA-256 of the payload if absent).
  Only keys not already active publish - so a condition fires once, not every
  tick it stays true.
- A condition is recorded active **only after its event actually publishes**, so
  a failed publish (event server briefly down) retries next interval instead of
  being lost.

Scheduler-owned state (last-run times, active condition keys) lives in
`run/state/monitor_state.json`, rewritten wholesale each tick.

---

# Script cache: paying once for a check, then running it free

The `script_cache` flavor is the token-saving path. A normal description-only
monitor spends an LLM call **every interval** to re-reason the same check. A
`script_cache` monitor spends that call **once**: the agent figures out the
right tool calls, runs the check, and emits a deterministic shell script that
reproduces it. Every later tick executes the cached script in a sandbox at ~$0 -
no model in the loop. If the script ever fails, the runner falls back to the
agent to fix and re-cache (self-healing).

A monitor whose entire config is a sentence:

```yaml
- name: unread-emails
  check: script_cache
  prompt: "Check my email for unread messages"
  id_field: id
  interval: 5m
  event: monitor/email.received
```

Implementation: `bobi/monitors/script_cache_checks.py`.

## Lifecycle

```
tick
 │
 ├─ active script exists & fingerprint matches?
 │    YES → re-verify integrity → run sandboxed ($0)
 │            ├─ exit 0 + parseable JSON → conditions     [mode: cached]
 │            └─ fail / garbage          → fall to self-heal ↓
 │    NO  → (regen backoff active? skip this tick) → self-heal ↓
 │
 └─ self-heal: agent runtime discovers tools, runs the check,
      emits this tick's items AND a candidate script
        ├─ candidate passes validation + smoke run
        │     ├─ auto mode / inside known envelope → pin it   [mode: first_gen]
        │     └─ review mode / new capability      → queue to pending/
        └─ candidate rejected → use the agent's items anyway, bump fail counter
```

A self-heal tick is never wasted: the agent both produces this tick's result and
emits the script for next time.

## What it caches: an LLM wrote this script

The load-bearing fact is that a cron now runs **machine-generated code
unattended with the manager's secret env**. The security model is the point of
the module, not an add-on. Defense in depth, all four must clear before a script
runs unattended:

1. **Generation constraints** (prompt-level) - the agent is told the script must
   be read-only, take no arguments, use only allowlisted binaries, print only a
   JSON list. This is a usability aid for self-correction, *not* the control.
2. **Static validation gate** (the control - `validate_script`) - enforces the
   shebang (`#!/usr/bin/env bash` + `set -euo pipefail`), a binary allowlist
   (`venn`, `gh`, `jq`, `cat`, `grep`, `sort`, … - notably **no** `curl`,
   `python3`, `sed`), and a side-effect deny scan (no redirection, no
   `rm`/`mv`/`mkdir`, no `sudo`/`eval`/`exec`, no command substitution, no
   package managers, no `git push`, no `venn --confirm`, no mutating `gh`
   verbs). Any unknown binary or construct is rejected, not waved through. Bash
   is Turing-complete, so this is best-effort, not a proof.
3. **Runtime sandbox** (`run_sandboxed` - belt and suspenders) - a fresh temp
   CWD with `HOME`/`TMPDIR`/`XDG_*` redirected into it, rlimits (10 MB writes,
   1 GB address space, 30 CPU-s, 64 procs, no core dumps), a 60 s timeout, and
   the whole scratch tree deleted after. The sandbox is the real boundary.
4. **Approval + post-hoc notification** - the gate decision is
   **proceed-but-notify**: in the default `auto` mode a script that passes
   validation and a smoke run is pinned and runs without a human gate, but
   **every first run emits a real `monitor/script.first_run` event** through the
   same wire as findings. The manager relays it; the per-monitor state sidecar
   logs it. The post-hoc observability is the safety trade for dropping the
   pre-approval gate.

Install-level defaults live under a `script_cache:` key in `agent.yaml`; any
per-monitor `extra` key overrides them:

| key | default | meaning |
|---|---|---|
| `prompt` | (required) | natural-language description of the check |
| `id_field` | `id` | dedup key field |
| `approval` | `auto` | `auto` pins directly · `review` queues for a human · `off` never persists (always agent runtime) |
| `allow_http` | `false` | allow raw `curl` GETs |
| `http_hosts` | `[]` | host allowlist when `allow_http` is on |
| `max_age` | unset | refresh a pinned script older than this |
| `on_persistent_failure` | `degrade` | `degrade` (backoff) or `pause` after repeated regen failures |

## Pinning, trust, and the capability envelope

A candidate is **pinned** only after it passes validation *and* a smoke run that
yields a parseable list - a script that exits 0 but prints garbage is rejected.
Pinning is atomic (`tmp → os.replace`), so a tick never sees a half-written
script.

Each pinned script records a **capability envelope**: the exact set of binaries,
Venn tools, and hosts it used, captured at approval time. On self-heal, a
regenerated script that stays *inside* the envelope (introduces no new
capability) auto-promotes; one that reaches for something new re-enters `review`
even in `auto` mode. So self-healing can fix a broken script but cannot silently
widen what the cron is allowed to touch.

Trusted state - the content `sha256`, the envelope, and observability counters -
lives in a per-monitor sidecar (`<name>.state.json`) next to the script, **not**
in `monitor_state.json`. The scheduler rewrites `monitor_state.json` wholesale
each tick, so a check runner writing the same file would be clobbered; the
sidecar avoids that race and keeps the trust record co-located with the script
it protects. Before each cached run the runner re-reads the script, checks its
hash against the sidecar, and re-validates - closing the verify→execute TOCTOU
window by running the exact verified bytes.

## When a script is regenerated

A pinned script is reused until one of these forces a regen:

- **No active script** - first run is always agent runtime.
- **It fails** - non-zero exit, timeout, or unparseable output triggers one
  self-heal attempt that tick.
- **Fingerprint mismatch** - the config changed. The fingerprint is
  `sha256(prompt + id_field + relevant extra)`; a mismatch marks the script
  stale.
- **Explicit invalidation** - `bobi agent <name> monitors recache <monitor>`
  deletes the script + state; the next tick regenerates.
- **`max_age`** - if set, a script older than this refreshes on the next tick.
  Unset by default: determinism over freshness.

If regens keep failing, a circuit breaker trips after 3 consecutive failures:
`degrade` backs off exponentially (capped ~6 h) and fires
`monitor/script.failing`; `pause` stops the monitor and fires the same event.
The counter and backoff reset on any successful cached run or pin.

## On-disk layout

```
$BOBI_HOME/agents/<name>/run/state/
├── monitor_state.json            # scheduler-owned: last-run, active keys
└── scripts/
    ├── <monitor>.sc.sh           # active pinned script (executable)
    ├── <monitor>.state.json      # trusted sidecar: sha256, envelope, counters
    └── pending/
        └── <monitor>.sh          # candidate awaiting human approval (review mode)
```

## Operating it

```bash
bobi agent <name> monitors list                      # last_mode, cached_runs, total agent cost
bobi agent <name> monitors recache <monitor>         # invalidate + force regen next tick
bobi agent <name> monitors approve-script <monitor>  # promote pending/ → active (review mode)
bobi agent <name> costs --by role                    # agent-runtime spend attributed to role=monitor
```

Each tick records a mode in the sidecar: `cached` ($0, ran the script),
`first_gen` (generated and pinned), or `fallback_regen` (fell back to the agent
after a cached failure). A healthy `script_cache` monitor sits at `cached` with
a flat cost line; recurring `fallback_regen` means the script keeps breaking -
check the prompt or the underlying tool.
