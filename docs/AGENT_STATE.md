# Agent state: running, stopped, and the third one

The agent page answers one question before any other: **is this thing
working?** For most of Bobi's life that was answered by a pidfile — a live pid
meant running, a missing one meant stopped. The dashboard card read it that
way, and so did `health_summary`, which hardcoded `healthy` to mean
"process exists".

A pid is not the answer. A manager process can be alive while the manager is
not working: wedged on a turn, `SIGSTOP`ped, out of file descriptors, deadlocked
on a lock it will never get. All of those passed the pid check and rendered
green, which is the single worst failure this surface can have — the state line
is the first thing read, and a green one is believed.

So state is a tri-state, and the third value costs a probe.

| state | meaning | how it is decided |
|---|---|---|
| `running` | the agent is up and answering | live pid **and** its health endpoint answers |
| `stopped` | the agent is not up | no live pid in the manager pidfile |
| `not_responding` | the process is alive, the agent is not | live pid, probe does not answer |

The fold lives in `bobi/webapp/health.py`; `LocalRuntime.health_summary` calls
it and `GET /api/agents/{name}/health` serves the result.

## The probe has three outcomes, not two

The manager runs a small health server (`bobi/manager_health.py`) and writes
its port to `run/state/manager-health.port`. The fold reads that file and asks
`GET /health`.

The outcome that matters is the one in the middle:

- **answered** → `running`.
- **did not answer** → `not_responding`.
- **there was nothing to ask** — no port file, or an unreadable one → also
  `running`, decided from the pidfile alone, with `detail` saying so.

That last case is load-bearing. A manager from an older build, or one whose
health server failed to start, publishes no port file, and treating a missing
file as a failed probe would flip every such agent to NOT RESPONDING on the
strength of an absence. **"Never asked" is not "asked and got nothing."** The
same distinction is why `probe_ok` in the fold is a tri-state (`True` / `False`
/ `None`) rather than a boolean, and why `manager.healthy` is false only on an
actual failed probe.

Liveness is decided **before** the probe, so a stale port file left behind by
an unclean exit cannot resurrect a stopped agent.

The probe is on the page's polling path, so it is bounded by
`PROBE_TIMEOUT_SECONDS` (1.5s). A stopped process refuses the connection
instantly; a `SIGSTOP`ped one lets the kernel complete the handshake from the
backlog and then says nothing, which is exactly what the timeout is for.

## Strip telemetry: `segments`

The status strip's readings ride along as an ordered `segments` list:

```json
{"key": "uptime", "label": "Uptime", "kind": "duration", "value": 7200.0, "note": ""}
```

`kind` says how to read `value` — `duration` (elapsed seconds), `time` (epoch
seconds), `count`, or `text`. Which segments appear depends on state:

- **running** — Uptime · Manager pid · Live runs · Last activity
- **not_responding** — Manager pid (noted `alive`) · Health probe · Last activity
- **stopped** — Since · Exit · Was up, from the manager's last **terminal**
  registry record

Two rules govern the list, and both are about not lying:

**A reading this machine cannot produce is omitted, never faked.** A manager
in its boot window has no start time, so there is no Uptime segment — not a
zero, not an "unknown". An agent that has never run has no terminal record, so
its strip says STOPPED and carries no segments at all, which is the whole truth
about it. Callers render the list they are given; there is no fixed set.

**A non-terminal record produces no Exit.** A manager whose process is gone but
whose registry record was never closed still reads `idle`. Rendering that as
`Exit: idle` — a live-sounding word for a dead process — is precisely the
confident wrong reading these segments exist to avoid, so the segment is
dropped instead.

Values go out raw and the browser formats them. It knows the viewer's timezone;
the server may not share it, and in hosted mode it usually does not.

### What is deliberately not here

The prototype's NOT RESPONDING strip shows `Health probe: failing 12m`. That
duration is not built. Knowing how long a probe has been failing requires a
memory of when it first failed, and this process has none — it answers each
request from disk and holds nothing between them. A stopwatch that started
whenever the browser happened to poll would be a fabricated number in the one
place the page is asked to be precise. The probe segment stays qualitative
(`no answer on :36981`), and LAST ACTIVITY — a real recorded fact — carries the
"how long has this been wrong" signal.

## Compatibility

`state`, `detail`, and `segments` are newer than the `TeamRuntime` interface,
and `health_summary` is implemented out of tree as well as here. A runtime that
predates these keys returns the older shape, so the endpoint passes every
payload through `health.normalize`, which fills them in (deriving `state` from
`manager.running`) without overwriting a runtime that reports its own.

That keeps the interface's own rule — *fields a runtime cannot know are
null/empty rather than omitted, so render code branches on value, never on key
presence* — true for every runtime, without requiring the out-of-tree one to
ship first.
