# 933 - Agent start / stop / restart lifecycle events in the event queue

Spec for [#933](https://github.com/moda-labs/bobi-agent/issues/933).
Status: awaiting build approval (Gate 1).

Every code citation below was produced by running the printed command against
`origin/main` at `5d2cf04` and reading the hit.
No line number in this document was written by hand.
Revision 3 rebased the spec onto `5d2cf04` and re-derived every reference: the
previous revision was based on `b5388bb`, which predates #979 (14 zero-consumer
names deleted) and #980 (OTel telemetry), and most of its citations had drifted.

## 1. Problem

The event timeline shows inbound events, manager decisions, and session
lifecycle.
It does not show when the agent manager starts, stops, or restarts, so an
operator cannot tell whether downtime was intentional, who asked for it, or
whether a start command actually reached a healthy manager.

Hosted deployments already have half of this.
The supervisor sidecar emits `manager_started` / `manager_stopped` /
`manager_restarted` onto `fleet/lifecycle`
(`bobi/supervisor/supervision.py:389,441,453,462,671,694`), the Worker folds
them into a bounded 48h trail (`event-server/worker/src/fleet.ts:120,247-256`),
and `EventBusRuntime.health_summary` renders that trail
(`bobi/webapp/event_bus.py:525-576`).

Local deployments have none of it.
`LocalRuntime.health_summary` returns `"lifecycle": []` literally
(`bobi/webapp/runtime.py:839`), and the local start/stop/restart paths
manipulate the process directly with no record anywhere.

And even on a hosted box the record is not durable: every lifecycle edge is a
best-effort bus publish (`bobi/supervisor/telemetry.py:112-118,208-212`), so a
broker outage erases the operational record of exactly the incident an operator
most needs to reconstruct.

## 2. Solution

Add a durable, append-only lifecycle journal at `run/state/lifecycle.jsonl`,
written directly to disk by whichever process **observed the effect**, never
through the event bus.
Reuse the supervisor's existing `fleet/lifecycle` vocabulary and payload shape
as the canonical contract for its entries.

Then unify the reads: one fold projects journal entries and hosted
`fleet/lifecycle` edges into one row shape, and `bobi agent events`, the
`health_summary` lifecycle trail, and a new event-queue read model all render
from that single fold.

The unit the journal records is a **manager process generation**, not service
state.
§5.3 says why that distinction is the design's load-bearing one rather than a
technicality.

## 3. Load-bearing findings

Each of these is a premise the design rests on. Each was verified in the tree,
not inferred.

**F1. `correlation_id` already exists in the canonical contract, and nothing
uses it.**
`Telemetry._emit_lifecycle` accepts and serializes `correlation_id`
(`bobi/supervisor/telemetry.py:196-206`), but a repo-wide grep finds no caller
that passes one:

```
$ rg -n 'correlation_id' -g '!CHANGELOG.md' -g '!docs/specs/**'
bobi/supervisor/telemetry.py:196:    def _emit_lifecycle(self, event: str, *, correlation_id: str | None = None,
bobi/supervisor/telemetry.py:203:        if correlation_id is not None:
bobi/supervisor/telemetry.py:204:            payload["correlation_id"] = correlation_id
```

So the field name for "one ID linking request and effect" is already chosen by
the contract. This spec fills it rather than inventing `operation_id`.

**F2. The admin command id never reaches the lifecycle edge.**
`admin.py` dispatches `restart` / `stop` / `start` by calling
`request_manager_restart()` / `request_manager_stop()` /
`request_manager_start()` (`bobi/supervisor/admin.py:258,261,264`), and those
setters take no arguments (`bobi/supervisor/supervision.py:399-409`).
The `command_id` the operator polls (`bobi/supervisor/admin.py:232`) is
therefore unlinkable to the edge the command produced.

**F3. No local start path waits for readiness.**
The CLI `start` command calls `service.spawn_team` (`bobi/cli.py:422`) and
`LocalRuntime.start_team` calls it too (`bobi/webapp/runtime.py:597`).
`spawn_team` "Spawn[s] the manager detached and return[s] without waiting for
registration" (`bobi/service.py:340-346`).
The only code that waits for registration *and* transport readiness is
`service.start_team` (`bobi/service.py:434-449`), via
`_wait_for_manager_entry` (`bobi/service.py:301`) and
`_wait_for_manager_transport` (`bobi/service.py:318`) - and its only caller is
`launch_team` (`bobi/service.py:458`), reached from two unit tests
(`tests/test_service.py:42,84`) and one integration test
(`tests/integration/test_team_instructions.py:69`).
`service.restart_team` (`bobi/service.py:722-729`) has no callers at all; this
was already recorded as Q031 in `plans/2026-07-22-review-remediation.md:204`.

Consequence: **no operator-facing process observes a local start completing**,
so "record `manager_started` after registration and transport readiness"
cannot be satisfied by instrumenting the requester.

**F4. `stop_team` observes the confirmed effect of the stops that go through
it - and two operator paths do not go through it.**
It polls up to 6s for `ProcessLookupError` and reports a kinded outcome -
`stopped` / `killed` / `stale` / `invalid_pid` / `permission_denied` /
`still_running` (`bobi/service.py:668-719`, `StopResult` at
`bobi/service.py:108-118`).
Both *unmanaged* operator stop paths reach it (`bobi/cli.py:1039`,
`bobi/webapp/runtime.py:610`).

But `bobi agent <name> stop` returns before that call whenever a systemd user
unit is installed:

```
$ rg -n '_has_systemd_service\(\)|_systemctl\(' bobi/
bobi/cli.py:279:def _has_systemd_service() -> bool:
bobi/cli.py:294:def _systemctl(action: str) -> bool:
bobi/cli.py:1031:    if _has_systemd_service() and not force:
bobi/cli.py:1033:        _systemctl("stop")
bobi/cli.py:1074:    if _has_systemd_service():
bobi/cli.py:1081:        _systemctl("restart")
```

`stop` shells out to `systemctl stop` and returns at `bobi/cli.py:1031-1034`;
`restart` shells out to `systemctl restart` and returns at
`bobi/cli.py:1074-1089`.
Neither reaches `stop_team`.
A design in which `stop_team` is the only stop writer therefore records
**nothing at all** on a systemd box, which is the deployment shape most likely
to be running unattended.
This is why §5.3 puts the primary stop writer inside the manager instead.

**F5. Restart is two independent calls on both unmanaged local paths.**
CLI: `ctx.invoke(stop)` then `ctx.invoke(start, fresh=fresh)`
(`bobi/cli.py:1092-1093`).
Webapp: `service.stop_team(root)` then `self.start_team(name)`
(`bobi/webapp/runtime.py:623,628`).
Nothing links the two halves today.

**F6. `child_agent_env` inherits the parent environment, and already strips one
inherited-id hazard.**
It documents stripping `BOBI_LAUNCH_LINEAGE` because "an agent running the
routine `bobi agent <x> restart` copies its own chain into the new manager
daemon" (`bobi/env.py:140,146-155`).
A correlation id passed by env has exactly the same hazard and gets exactly the
same treatment (see §5.4).

**F7. The manager child is spawned through two hops.**
Supervisor spawns `bobi agent <name> start`
(`bobi/supervisor/supervision.py:269-277`), which spawns
`bobi agent <name> start --foreground` with `child_agent_env`
(`bobi/service.py:377-378`).
An env-carried id survives both hops for free.

**F8. `run/state/lifecycle.jsonl` has no name collision.**
`state_path()` is `<run>/state` (`bobi/paths.py:207-208`).
Enumerating its children mechanically finds no `lifecycle*` path:

```
$ rg -n 'state_path\((.*)\) / "' -g '*.py' bobi/ | wc -l
19
$ rg -n 'state_path\((.*)\) / "' -g '*.py' bobi/ | rg -c lifecycle
0
```

The 19 hits are `monitor_runs`, `deployments` (x2), `cursors`, `bubble.json`,
`manager.pid`, `long_term_memory.md`, `long_term_memory_cursor`, `sessions`,
`monitor_state.json`, `kb`, `format_version`, `spend_governor.json`,
`manager-health.port` (x2), `workflow/runs`, `admin-cursor.json`,
`decisions.jsonl`, and `scripts`.
Plus the `events-*.jsonl` glob (`bobi/cli.py:2178`, `bobi/doctor.py:638`),
which `lifecycle.jsonl` does not match.
Neither does the `lifecycle.jsonl.lock` sibling `file_lock` will create (§5.2).

**F9. `bobi agent events` IS tested, through the CLI surface, and the five
behaviours it pins are the contract this spec must not break.**
The previous revision claimed this command had no coverage, on the strength of
`rg -n '_show_events' tests/` returning nothing.
That grep was a false negative: the tests drive the click command, never the
private function.

```
$ rg -n 'class TestEventsCommand' tests/test_cli.py
414:class TestEventsCommand:
$ rg -n '    def test_' tests/test_cli.py | awk -F: '$1>=414 && $1<=476'
418:    def test_skips_malformed_lines_in_events_jsonl(self, bobi_install):
432:    def test_skips_malformed_lines_in_decisions_jsonl(self, bobi_install):
443:    def test_deduplicates_events_by_seq_deployment(self, bobi_install):
452:    def test_payload_event_renders_text(self, bobi_install):
465:    def test_ignores_legacy_events_jsonl(self, bobi_install):
```

Five tests invoke `bobi agent <name> events` and assert on its output; a sixth
in the same class covers `events publish` and is unrelated.
So moving the fold out of `_show_events` (`bobi/cli.py:2169-2247`) into a
tested module is a **refactor under existing tests**, not the coverage rescue
the previous revision described.
§7 states the consequence: those five keep passing unchanged, and that is the
acceptance bar for the move.

**F10. A local event-queue *view* is a standing non-goal, and lifecycle is not
what that non-goal excludes.**
The merged single-agent-view plan lists "*Decisions and raw event deliveries
stay CLI-only* (`bobi agent events`) - they are log lines, not runs (no status,
no cost)" (`plans/2026-07-31-single-agent-view.md:135-136`) and repeats
"decisions/raw events in the UI" under Non-goals (`:316`).
Issue #933 argues the opposite category on the same axis: lifecycle entries are
not runs *and* not log lines, they "explain changes to the runtime that performs
that work".
§4 resolves this explicitly rather than quietly overriding an approved plan.

**F11. Both `TeamRuntime` implementers are in-tree, so widening the ABC is
safe.**

```
$ rg -n 'TeamRuntime\)|\(TeamRuntime' bobi/ tests/ agents/
```

Two subclasses, `LocalRuntime` (`bobi/webapp/runtime.py:551`) and
`EventBusRuntime` (`bobi/webapp/event_bus.py:175`); every other hit is an
exception type deriving from `TeamRuntimeError` (`bobi/webapp/runtime.py:33`).
No test defines a subclass.
That is exactly the condition the ABC's own widening rule requires before a new
`@abstractmethod` may be added (`bobi/webapp/runtime.py:89-94`), so §5.6's
`events()` cannot break an implementer this repo cannot see.

**F12. The host's own deployment identity leaks into tests.**
A container running as a deployed agent exports `BOBI_INSTANCE`, `BOBI_FLEET`,
and `FLY_APP_NAME`, and `resolve_deployment_identity` reads exactly those:

```
$ rg -n 'BOBI_FLEET|BOBI_INSTANCE|BOBI_AGENT|FLY_APP_NAME|def resolve_deployment_identity' bobi/identity.py
42:    if os.environ.get("FLY_APP_NAME"):
54:    instance = _first_env("BOBI_INSTANCE", "BOBI_AGENT")
67:def resolve_deployment_identity(project_root: Path | None = None) -> dict:
92:        "fleet": _first_env("BOBI_FLEET") or "default",
```

On such a host,
`tests/test_supervisor_alerting.py::TestSoftAlert::test_second_charged_restart_posts_once`
and `::TestExhaustionAlert::test_at_cap_alert_is_marked_terminal` fail against
unmodified `main`, because they assert on a fixture instance name and get the
host's. Proven rather than inferred: re-running those two with
`env -u BOBI_INSTANCE -u BOBI_AGENT -u FLY_APP_NAME` passes both.

Citation note, because the previous revision got this wrong twice and the
finding is only worth as much as its reference: the reads are in
**`bobi/identity.py`**, not `bobi/supervisor/identity.py`.
The latter is a 25-line re-export shim as of #980; the range the previous
revision cited (`:60-91`, then `:70-91`) does not exist there and pointed at
unrelated lines at the older base.

This matters here because §5.1 reuses that same function for the journal's
`deployment` block, so it is a standing requirement on the tests this spec
adds (§7), not a curiosity. With those vars cleared, the function returns
`{"fleet": "default", "instance": "<run-root basename>", "platform":
"unknown", ...}` - the local case §5.1 depends on, confirmed by running it.

**F13. This repo ships no systemd unit for the manager, so no `Restart=`
policy is knowable from inside the box.**

```
$ find . -name '*.service' -not -path './.git/*'
(no output)
$ rg -n 'Restart=|\[Service\]|WantedBy=' --glob '!.git' .
docs/SELF_HOSTED_EVENT_SERVER.md:175:[Service]
docs/SELF_HOSTED_EVENT_SERVER.md:178:Restart=always
docs/SELF_HOSTED_EVENT_SERVER.md:182:WantedBy=multi-user.target
```

The one unit in the tree is for the **event server**, a different process.
`_has_systemd_service` looks for `~/.config/systemd/user/bobi.service`
(`bobi/cli.py:281`), which the operator authors.
So the box can observe that its manager process is terminating; it cannot
observe whether the service manager intends a replacement.
§5.3 makes that boundary explicit instead of claiming past it.

**F14. `fsutil.file_lock` blocks without bound.**
It takes `fcntl.flock(..., LOCK_EX)` with no timeout and no `LOCK_NB` anywhere
in the repo (`bobi/fsutil.py:180`; the only other `LOCK_EX` call sites are
`bobi/sdk.py:161` and `bobi/launch_admission.py:358`, both equally unbounded).
A lifecycle writer that inherits that behaviour can hang a start or a stop
behind a wedged lock holder, which is the same fail-open violation as V6 in a
different costume. §5.2 specifies a bounded acquisition.

**F15. Every pid-liveness check in the repo is a bare `os.kill(pid, 0)`.**

```
$ rg -n 'os\.kill\(pid, 0\)' bobi/
bobi/sdk.py:133:        os.kill(pid, 0)
bobi/supervisor/probe.py:33:        os.kill(pid, 0)
bobi/webapp/daemon.py:96:        os.kill(pid, 0)
bobi/cli.py:993:        os.kill(pid, 0)
bobi/cli.py:1009:            os.kill(pid, 0)
bobi/service.py:686:                os.kill(pid, 0)
bobi/service.py:698:                        os.kill(pid, 0)
```

Every one of them checks a pid written seconds earlier, so pid reuse is not a
live risk for any of them.
The reconciler §5.3 introduces is the first caller to check a pid that may be
up to a retention window old, which is exactly the regime where reuse becomes
likely. §5.3 therefore does not reuse this pattern unmodified.

## 4. Scope

### In scope

1. `bobi/manager_lifecycle.py`: the journal (schema, append, reconcile,
   retention) and the read fold (collapse, projection).
   Named `manager_lifecycle`, not `lifecycle`, because `LIFECYCLE_EVENTS` in
   `bobi/events/subscriptions.py:64` already means *session* lifecycle
   (`agent/session.completed`). Two "lifecycle" vocabularies in one package is
   how a reader ends up debugging the wrong one.
2. Local writers: a manager-side stop confirmer in the manager's existing
   shutdown path, a manager-side start confirmer, `service.stop_team` stop and
   stop-failure entries, start-failure entries at the two operator entry
   points, restart correlation on both unmanaged restart paths.
3. Supervisor: thread the admin `command_id` into `correlation_id` on operator
   edges (F2), and journal every supervisor edge locally through a new
   observer.
4. Reads: fill `LocalRuntime.health_summary()["lifecycle"]`, fold the journal
   into `bobi agent events`, and add `GET /api/agents/{name}/events` on both
   runtimes.
5. One visible local surface: a `last_transition` entry in the status strip's
   existing `segments` list (§5.6). No new component, no new frontend code.
6. A bounded-acquisition option on `fsutil.file_lock` (F14), defaulting to
   today's blocking behaviour so no existing caller changes.
7. Docs: `docs/ADMIN_PROTOCOL.md` (additive fields), `docs/AGENT_STATE.md`
   (local trail is now populated), a new `docs/LIFECYCLE_JOURNAL.md`.
8. Tests per §7.

### Out of scope, and why

- **launchd parity on macOS.**
  There is no launchd integration in this repo to confirm effects through:
  `rg -ni 'launchd|launchctl|LaunchAgents|KeepAlive' bobi/` returns nothing,
  and `_has_systemd_service` (`bobi/cli.py:279`) has no macOS counterpart, so
  `bobi agent stop|restart` signals the process directly and a KeepAlive job
  respawns it underneath. That is a real bug and it is already tracked as
  **MOD-352** ("launchd lifecycle parity: `bobi agent stop|restart` fight a
  macOS service manager (systemd branch has no counterpart)", GitHub
  [#925](https://github.com/moda-labs/bobi-agent/issues/925)).
  This spec does not fix it and does not model it.
  What it does do is make it visible: §5.3's generation model records the
  manager's own termination and the replacement's own start as two entries
  seconds apart with no operator correlation id between them, which is
  precisely MOD-352's signature. The journal becomes the diagnostic for that
  ticket rather than a second implementation of it.
- **A full event-queue *list view* in the local `bobi app` UI.**
  F10: an in-UI event/decision queue is a standing non-goal of the merged
  single-agent-view plan. What this spec ships instead is the read model, the
  *existing* rendering seam (`health_summary()["lifecycle"]`, the key both
  runtimes fill and the hosted console already consumes), and one status-strip
  segment so the local page is not left with a populated-but-invisible
  payload. An operator on `bobi app` sees the last transition; the full history
  is `bobi agent events` and the `/events` endpoint.
- **The hosted console's event-queue view.**
  The console is private (`moda-labs/moda-agents`). This repo ships the API it
  reads; the console-side render is MOD-261's work (§11).
- **`event` / `decision` rows on the new endpoint.**
  The row shape carries `kind` so they can be added without a contract change,
  but only `kind: "lifecycle"` is emitted here. Adding the other two would
  require a box-consulting admin command for hosted and would contradict F10.
- **The Runs table.** Unchanged. `bobi/webapp/runs.py:12-14` already states the
  exclusion; this spec adds a line naming lifecycle explicitly.
- **`service.restart_team`.** Still zero callers (F3). Not adopted, not
  deleted here; deleting it is Q031's job, not this issue's.
- **Version / changelog.** Untouched, per the repo's release rules.

## 5. Technical approach

### 5.1 The canonical contract, reused verbatim

A journal entry **is** a `fleet/lifecycle` payload
(`bobi/supervisor/telemetry.py:196-206`) plus three additive fields:

```json
{
  "deployment": {"fleet": "default", "instance": "eng-team",
                 "platform": "unknown", "machine": null,
                 "region": null, "node": null},
  "event": "manager_started",
  "generated_at": "2026-08-06T04:10:22.114233+00:00",
  "correlation_id": "9f2c1d84b7ae4f5390cc61d2a7e5b310",
  "reason": "operator",
  "manager_pid": 4711,
  "phase": "start",
  "origin": "manager",
  "generation": {"pid": 4711, "start_token": "8419273"}
}
```

- `deployment` comes from `resolve_deployment_identity`
  (`bobi/identity.py:67-98`), which already resolves for an unsupervised local
  agent (`fleet: "default"`, `instance` from the run-root basename,
  `platform: "unknown"`) - run, not assumed (F12). One definition, not two.
  Note F12's other half: the same function makes the host's identity leak into
  any test that does not clear the env.

  The previous revision imported this from `bobi/supervisor/identity.py`
  function-locally, to keep a core module off the sidecar's import path, and
  recorded "hoist identity to `bobi/identity.py`" as a rejected alternative.
  **That alternative has since landed**: #980 moved the implementation to
  `bobi/identity.py` and left `bobi/supervisor/identity.py` as a 25-line
  re-export shim.
  So there is nothing to reject and nothing to work around - `bobi.identity` is
  a plain core module and `bobi/manager_lifecycle.py` imports it normally, at
  module level. The supervisor's dependency discipline
  (`bobi/supervisor/__init__.py:14-19`) is untouched because the arrow now
  points the way that discipline always wanted.
- `phase` (`"start" | "stop" | "restart"`), `origin`
  (`"manager" | "operator" | "supervisor"`), and `generation` are the three
  additive fields.
  Additive is free under the protocol's compatibility promise
  (`docs/ADMIN_PROTOCOL.md:27-30`), so `SUPERVISOR_VERSION` does not move.
  The Worker tolerates them without a change for the same reason: its
  `LifecyclePayload` declares `[k: string]: unknown`
  (`event-server/worker/src/fleet.ts:46-51`) and stores the payload whole
  (`:254`).

**`generation` is the identity of one manager process**, and it is the field
three separate problems turn on: distinguishing a stop from a replacement's
start (§5.3), deduplicating two writers who saw the same stop (§5.5), and
surviving pid reuse (§5.3). It is a pid plus an opaque start token:

- Linux: field 22 of `/proc/<pid>/stat`, the process start time in clock ticks
  since boot. Parsed by splitting on the **last** `)`, because the comm field
  can contain spaces and parentheses. This is the same "read `/proc`, take no
  dependency" approach `bobi/supervisor/snapshot.py:69-76` already uses.
- Elsewhere (macOS, BSD): `ps -o lstart= -p <pid>`, taken as an opaque string.
- Neither available: `null`.

`start_token` is **compared for equality and never parsed, ordered, or
displayed**. That is what lets one field be a tick count on one platform and a
date string on another without a per-platform branch anywhere else in the
design. A `null` token means the guard was unavailable on this host, and it is
recorded as `null` rather than omitted so a reader can tell "same process" from
"could not check".

**Vocabulary.** The three confirmed effects keep the supervisor's names.
Failures get three new names in the same family, because a failure is a
distinct event and not a `manager_stopped` with a flag:

| Event | Written when |
|---|---|
| `manager_started` | registration **and** transport readiness both confirmed |
| `manager_stopped` | a manager process terminated, observed or reconciled |
| `manager_restarted` | derived by the fold from a correlated stop+start (§5.5) |
| `manager_start_failed` | a start attempt failed with a known reason |
| `manager_stop_failed` | the process would not exit, or could not be signalled |
| `manager_restart_failed` | derived: a restart whose start phase never landed |

### 5.2 The journal file

`run/state/lifecycle.jsonl`, one JSON object per line, newest last.

**Every write holds `fsutil.file_lock` for the whole open-append-close.**
That is stricter than the existing event-log writer's bare append-mode `open`
(`bobi/events/client.py:67-74`), and the extra strictness is load-bearing
rather than defensive.
Retention (below) prunes by rewriting the file through
`fsutil.atomic_write_text`, and that helper lands the new content as a **new
inode renamed over the target** - a hazard its own module docstring names
(`bobi/fsutil.py:29-38`).
An appender holding an fd on the pre-rename inode would write into an orphaned
file and silently lose its line.
Serializing every writer against the same lock closes that race; there is no
version of this design where a plain append and an atomic rewrite coexist
safely on one path.

`fsutil.file_lock` is the right primitive here rather than a hand-rolled one
for a reason worth stating, because it is the reason the fix works at all: it
takes the lock on a **companion `<name>.lock` file, not on the target**, and
its own docstring gives the rationale - "the target is replaced by rename on
every write [so] a lock held on the old inode would protect nothing once the
first writer landed" (`bobi/fsutil.py:165-167`).
Verified by reading the running implementation, not the call site.

Consequence to expect: the journal creates a sibling
`run/state/lifecycle.jsonl.lock`. That is a new file in `state/` and it
collides with nothing (F8).

**The acquisition is bounded.** `file_lock` today blocks forever (F14), and a
lifecycle writer that inherits that can hang the very start or stop it exists
only to describe.
`fsutil.file_lock` therefore gains an optional `timeout: float | None = None`.
`None` keeps today's blocking `LOCK_EX` exactly as it is, so
`spend_governor`, `setup/state`, and every other current caller is byte-for-byte
unaffected; a float retries `LOCK_EX | LOCK_NB` on a short sleep until the
deadline and then raises.
The journal passes `timeout=2.0` and treats expiry as a skipped write, logged
at debug. Two seconds is far beyond any honest contention for a file written a
handful of times a day, so expiry means a wedged holder, and the right answer
to a wedged holder is to lose one journal line rather than to hold up a stop.

This is an option on the shared helper rather than a fourth lock
implementation on purpose: the repo already carries three
(`bobi/fsutil.py:162`, `bobi/sdk.py:155`, `bobi/launch_admission.py:352`), and
CLAUDE.md's rule against hand-rolling a seventh atomic write is the same rule.

**Every write is fail-open.** A read-only state directory, a full disk, or a
lock timeout is logged and swallowed. A journal failure must never fail a
start, a stop, or a supervisor restart decision - the record exists to explain
operations, not to gate them.

The `try` must wrap **the lock acquisition too**, not just the write inside it.
`file_lock` does `lock_path.parent.mkdir(...)` and `open(lock_path, "a+")`
before it yields (`bobi/fsutil.py:178-179`), so on a read-only state directory
it raises *before* any guarded body runs. The naive shape

```python
with file_lock(path, timeout=2.0):   # raises here, uncaught
    try: ...
    except Exception: log.debug(...)
```

is fail-*closed* on exactly the case the rule exists for, and it would take a
start down. The guard goes outside.

**Multiple writers are expected**: the manager process, a CLI `stop`, a webapp
worker, and the supervisor can all append.

**Retention** is `MAX_LIFECYCLE_ENTRIES = 500` plus a time bound
`LIFECYCLE_RETENTION_S`, whose value is an open decision escalated in §9 and
**not settled by this spec**.
Pruning happens **at write time only**, under the same lock the append holds,
and only when the file exceeds the entry cap. Read paths never mutate.

**Prune order is fixed and stated**, because "prune by age then by count" and
"by count then by age" keep different entries and the difference shows up as a
missing incident:

1. append the new entry to the in-memory list;
2. drop entries older than `LIFECYCLE_RETENTION_S`, measured against the
   newest entry's `generated_at`, not against wall clock - a box that was off
   for a week must not wake up and delete its own last transition;
3. if more than `MAX_LIFECYCLE_ENTRIES` remain, keep the newest
   `MAX_LIFECYCLE_ENTRIES`;
4. rewrite through `atomic_write_text`.

Age first, then count: the cap exists for a restart-looping box, and applying
it first would let a burst of loop entries evict the older, quieter transitions
that explain how the loop started.

**`--fresh` does not wipe the journal.** `clear_manager_session` removes only
the saved session id, the bubble state, and the `deployments/` and `cursors/`
directories (`bobi/service.py:145-155`). That is correct: `--fresh` clears
conversation state, not the operational record of why the runtime moved.

### 5.3 What the journal records: generations, not service state

The previous revision's rule was "the writer is whichever process can observe
the effect", with `stop_team` as the single stop writer.
F4 shows that rule silently produces an empty journal on a systemd box, because
`bobi agent stop` and `bobi agent restart` return before `stop_team` whenever a
unit is installed.
Adding a systemd branch to `stop_team`'s callers would not fix it either, since
`systemctl stop bobi` typed directly, a container stop, and a supervisor
respawn all bypass the CLI entirely.

So the rule changes, and the fix is structural rather than a caveat.

**The unit of record is a manager process generation** - one pid with one start
token (§5.1) - and the primary observer of a generation ending is **the
generation itself**.

The manager already has the seam. `run_manager_from_config` registers a
`_cleanup` at `bobi/service.py:562-575` and installs a SIGTERM handler that
calls it at `bobi/service.py:577-582`.
That path already runs on `systemctl stop`, on `systemctl restart`, on
`stop_team`'s SIGTERM, on a container stop, on a supervisor respawn, and on a
bare `kill`, because all of them deliver SIGTERM to the manager process.
`_cleanup` even already guards on generation identity in its own small way, only
unlinking the pid file when it still holds this process's pid
(`bobi/service.py:565`).
It gains one journal write.

| Observation | Who can make it | Entry |
|---|---|---|
| this manager process is terminating | the manager, in `_cleanup` (`bobi/service.py:562-575`) | `manager_stopped`, `origin: "manager"` |
| registration + transport readiness reached | the manager, in the start confirmer thread | `manager_started`, `origin: "manager"` |
| the pid I signalled is gone, would not die, or could not be signalled | `stop_team` (`bobi/service.py:668-719`) | `manager_stopped` / `manager_stop_failed`, `origin: "operator"` |
| a start attempt raised before the child was up | CLI `start` and `LocalRuntime.start_team`, in their existing `except` arms | `manager_start_failed`, `origin: "operator"` |
| a supervisor decision | the supervisor's `JournalObserver` | the edge it emitted, `origin: "supervisor"` |
| a generation ended and nobody recorded it | the next writer's reconcile | `manager_stopped`, `reason: "reconciled"` |

`stop_team` stays a writer even though the manager now writes its own stop.
It is not redundant: it observes three outcomes the dying process by definition
cannot report (`still_running`, `permission_denied`, `invalid_pid`), and it
records the operator's `correlation_id` and intent, which the manager never
knows. §5.5 merges the two views of one stop into one row.
It reads the target's start token **before** signalling, since after the exit
there is no process left to read one from.

**The shutdown write happens once, and one flag enforces it.**
`_cleanup` is reached twice on the SIGTERM path: `_handle_term` calls it
directly and then raises `SystemExit`, which runs the `atexit` registration
(`bobi/service.py:575-580`). That double call is invisible today because every
existing step in `_cleanup` is idempotent - `unlink(missing_ok=True)`,
`manager_health.stop()`, `pooled_http.close()`. A journal append is not, so
adding one naively would record two `manager_stopped` lines for one
termination.

A module-level "shutdown recorded" flag, set under the journal's own lock,
makes the first call write and every later one a no-op. Two consequences it
also buys, which is why it is a flag rather than a moved call site:

- **the start confirmer checks the same flag before writing.** It is a daemon
  thread that may still be inside its 30s wait when SIGTERM lands, and without
  the check it could append a `manager_started` *after* the generation's own
  `manager_stopped`. With it, a confirmer that loses that race writes nothing,
  which is correct: the generation never reached readiness;
- **a normal exit is still covered.** Keeping the write inside `_cleanup`
  rather than inside `_handle_term` means Ctrl-C on `--foreground` and any
  clean return record too, since both reach `atexit`.

Belt and braces, stated because a flag is exactly the kind of thing a later
refactor drops: even if the guard were lost, §5.5's generation grouping folds
two `manager_stopped` entries for one generation into a single row, so the
visible history stays correct and only the file grows.

**The manager reads its own start token once, at boot, and caches it.**
On Linux that is a `/proc` read either way, but on macOS the token comes from a
`ps` subprocess (§5.1), and forking during shutdown - inside a SIGTERM handler,
on a path that may already be under memory pressure - is not something to do
for a log line. Caching at boot means the shutdown path only ever formats a
value it already holds. The reconciler is then the sole caller that reads
*another* process's token, and it runs at start time, not shutdown.

**The start confirmer.** A daemon thread started in `run_manager_from_config`
just before the blocking `spawn_adhoc` call (`bobi/service.py:657`).
It reuses `_wait_for_manager_entry` + `_wait_for_manager_transport`
(`bobi/service.py:301,318`) against its own root and session name, which are
the two predicates the issue's wording names. On success it writes
`manager_started` with its own generation; on timeout it writes
`manager_start_failed` with the timeout's reason. It is a daemon thread so it
can never hold shutdown, and it is fail-open.

Why the manager and not the requester: F3. The requester returns before
readiness on every local path, and changing that would make `bobi agent x
start` block for up to 30s and terminate a slow-but-healthy manager on timeout
(`bobi/service.py:441-449`). Writing from inside the manager also covers the
paths that have no requester at all - systemd, the container entrypoint, and a
supervisor respawn.

**Reconciliation covers what nobody could observe.** SIGKILL, an OOM kill, a
hard container stop, and a power loss all end a generation with no handler run.
Reconciliation runs in exactly two places, both under the §5.2 lock, so two
processes can never both append a reconciled stop for the same generation:

- inside `record()` **only when the entry being written is
  `manager_started`** - if the newest existing entry is a `manager_started`
  whose generation is no longer live, with no later stop, a `manager_stopped`
  with `reason: "reconciled"` is appended first. Scoping it to start records
  keeps every other write a single lock-and-append, and a start is precisely
  the moment the previous generation is provably over.
- `stop_team`'s existing stale-pid branch (`bobi/service.py:687-689`), which
  already detects the same condition and now records it.

**Liveness is generation liveness, not pid liveness.** This is the one place in
the repo that must not use the bare `os.kill(pid, 0)` of F15.
Every existing caller checks a pid written seconds ago; the reconciler checks
one that may be a full retention window old, and Linux recycles pids from a
32768-wide space, so on a busy box a stale pid can be alive and belong to
something else entirely. A bare liveness check would then read "the manager is
still running" and the reconciled stop would never be written - the failure is
silent and looks exactly like a healthy box.

A generation is live only when **both** hold:

1. `os.kill(pid, 0)` does not raise `ProcessLookupError`, and
2. the recorded `start_token` equals the token read for that pid right now.

If the recorded token is `null`, or the current token cannot be read, the check
degrades to (1) alone and the reconciled entry carries
`reason: "reconciled-unverified"` so the weaker basis is visible in the trail
rather than assumed away.

**This coupling is worth naming because it changes with a number §9 escalates.**
Pid-reuse exposure scales directly with retention: the longer the newest
`manager_started` may sit unreconciled, the more likely its pid has been
recycled. At the 48h bound the start-token check is cheap insurance. If
retention grows to a week or a month, it stops being insurance and becomes
load-bearing - the reconciler would be wrong often enough to matter without it.
Whichever retention §9's decision picks, the check ships.

**What the journal deliberately does not claim.** Whether a *service manager*
intends the agent to stay down. F13: this repo ships no unit for the manager,
so its `Restart=` policy is authored by the operator and unknown to the box, and
the same is true of a container restart policy and of launchd's `KeepAlive`.
A `manager_stopped` therefore means exactly "this manager process terminated"
and never "the agent is down and staying down". A respawn by systemd, by a
container runtime, by the supervisor, or by launchd shows up honestly as the
next generation's `manager_started`, seconds later, in the same trail.
That is the whole truth available on the box, and stating the boundary is what
keeps the design's "confirmed effects" claim true rather than aspirational.

### 5.4 Correlation ids

One id per operator *operation*, in the contract's existing field (F1).

**Format:** `uuid.uuid4().hex` - 32 lowercase hex characters, no dashes.
That is the repo's existing id shape (`bobi/webapp/runtime.py:689`,
`bobi/subagent.py:1065`, `bobi/monitors/run_records.py:119`), it needs no
dependency, and it needs no sortability because ordering comes from `at`
(§5.5), never from the id.
Hosted operations do not mint one at all: they carry the admin `command_id`
verbatim, whatever shape the console chose, and the field is typed as an opaque
string so both coexist.

- **Local restart** mints one id and threads it through both halves:
  `stop_team(..., correlation_id=op, phase="restart")` for the stop, and
  `BOBI_LIFECYCLE_CORRELATION_ID=op` in the child env for the start.
  **Only the id crosses the process boundary.** `phase` is set by whichever
  writer already knows the operation's shape - the stopper knows it is stopping
  for a restart; the manager's confirmer thread cannot know why it was spawned
  and is never asked to. §5.5's fold derives the restart from the pair, which
  is why no second env var is needed.
- **Hosted** uses the admin `command_id` as the id, closing F2:
  `request_manager_restart(correlation_id=...)` / `request_manager_stop` /
  `request_manager_start` carry it (`bobi/supervisor/supervision.py:399-409`),
  the operator handlers pass it to `observer.lifecycle(..., correlation_id=...)`
  (`:441,453,462`), and the respawn puts the same value in the child's env so
  the manager's own confirmation entry correlates with the supervisor's edge.
- **Everything else** mints a fresh id per entry, so every row has one and the
  fold never has to special-case a missing key.

**The inherited-id hazard, and its fix.** `child_agent_env` inherits the parent
environment (F6), so an agent that runs `bobi agent x restart` from a session
launched with the variable set would stamp a stale id onto an unrelated
manager. `child_agent_env` therefore **strips**
`BOBI_LIFECYCLE_CORRELATION_ID`, in the same clause and for the same reason it
strips `BOBI_LAUNCH_LINEAGE` (`bobi/env.py:140,146-155`), and the spawner
injects the fresh value into the returned dict immediately before spawning -
the pattern `launch_agent` already uses for the lineage stamp.

### 5.5 One row per operation: the fold

`bobi/manager_lifecycle.py::fold(entries)` groups entries and collapses each
group to one row. This is the single place the three-rows-for-one-restart
problem is solved, and the place two writers' views of one stop become one row.

**Grouping key.** `correlation_id`, with one addition the previous revision
lacked: two entries for the **same event on the same `generation`** join the
same group even when their correlation ids differ.
That case is not hypothetical, it is the normal path - the manager writes
`manager_stopped` from `_cleanup` carrying the id it was *started* with (or
none), while `stop_team` writes `manager_stopped` for the same generation
carrying the id of the *stop request*. Without the generation clause a single
`bobi agent stop` produces two rows.

**Field precedence.** When a group holds several entries describing one effect,
each field comes from the highest-precedence entry that has it, and the
precedence differs by field because the two writers know different things:

| Fields | Precedence | Why |
|---|---|---|
| `at`, `manager_pid`, `generation` | `manager` > `supervisor` > `operator` | the confirming process is the one that observed the moment |
| `reason`, `correlation_id`, `phase` | `operator` > `supervisor` > `manager` | intent was recorded by whoever asked |

That single rule replaces the previous revision's per-case field notes, and
rules 1 and 3 below are now instances of it rather than separate machinery.

Collapse rules, applied per group, in order:

1. **A group holding both a stop effect and a start effect is a restart.**
   The row is `manager_restarted`, with fields taken by the precedence table:
   `at`/`manager_pid` from the start (that is when the replacement became ready
   and which process it is), `reason` from the stop (that is where the
   requester's intent was recorded).
   This derivation is why no `phase` has to cross a process boundary (§5.4).
2. **A group holding only a stop that carries `phase: "restart"` is a failed
   restart.** The row is `manager_restart_failed` with that entry's reason.
   Rule 1 cannot infer this, which is the whole reason `phase` exists as a
   field: a restart whose replacement never came up is indistinguishable from a
   plain stop without it.
3. **Otherwise the row is the group's highest-ranked entry**, by this fixed
   rank: `manager_restarted` > `manager_started` > `manager_stopped` >
   any `*_failed`; ties inside a rank resolve by the precedence table.
4. **A single-entry group passes through unchanged**, which is every hosted
   `probe_failing`, `probe_recovered`, and `budget_exhausted` edge.

**`origin` is who wrote the entry, not who asked.** Intent lives in `reason`.
Stated flatly because rule 3 tiebreaks on `origin`, so an ambiguous assignment
would silently pick the wrong row's timestamp:

- every entry from the supervisor's `JournalObserver` is `origin: "supervisor"`
  - the operator edges (`bobi/supervisor/supervision.py:441,453,462`), the
  wedge and crash restarts (`:489,516,528`), boot and teardown
  (`:671,694`), `budget_exhausted` (`:349`), and the probe episodes
  (`bobi/supervisor/telemetry.py:186,193`). What differs across those is
  `reason` (`operator`, `crash`, `wedge`, `boot`, ...), never `origin`;
- every entry from the manager's own confirmer or `_cleanup` is
  `origin: "manager"`;
- every entry from `stop_team`, the CLI, or `LocalRuntime` is
  `origin: "operator"`.

Rule 3 is doing real work on pairs that occur constantly, not just in theory.
A supervised **operator restart** produces `manager_restarted` (the supervisor,
at respawn) and `manager_started` (the manager, at readiness) under one id, and
collapses to one honest row carrying the supervisor's reason and the manager's
confirmed timestamp. A supervised **boot or wedge restart** produces the same
pair. An unsupervised `bobi agent stop` produces the manager/`stop_team` stop
pair described under Grouping key.

Rule 3 is also the deduplication the issue asks for. Worth stating plainly
rather than overselling: a published copy and a locally journaled copy of the
same effect share a `correlation_id`, so they collapse - but **today no single
response carries both copies**, because `EventBusRuntime` reads only the
server-side trail and never the box's journal
(`bobi/webapp/event_bus.py:525-534`). The rule earns its place on the local
pairs above; it makes a future merged read correct for free.

**Ordering.** Newest first, on `received_at` when present and `at` otherwise,
both compared as epoch seconds.

The previous revision claimed this matched `listLifecycle` by keying on
`generated_at`. It does not, and the mismatch matters:
`listLifecycle` sorts on `received_at` descending
(`event-server/worker/src/fleet.ts:185`), which is server receipt time, and
the Worker calls it "authoritative for reachability" (`:55`).
Local rows have no `received_at` at all - §5.6 sets it null, because nothing
received them.
So a single key cannot order both sources, and the honest rule is the fallback
one above. Its properties are worth stating because they are the reason to
prefer it over picking one key and pretending:

- on a hosted-only list every row has `received_at`, so it reproduces
  `listLifecycle`'s order exactly;
- on a local-only list no row has one, so it orders by the box's own clock,
  which is the only clock those entries ever had;
- on a merged list each row is placed by the best timestamp it carries, and
  because clock skew between a box and the server is real, the projection
  documents that a merged ordering is approximate rather than claiming a
  precision it cannot deliver.

### 5.6 Reads

`bobi/manager_lifecycle.py::to_rows(entries)` is the one projection. All three
read surfaces call it, which is what makes "the CLI and the UI show the same
history" mechanical rather than a promise.

**`bobi agent events`** (`bobi/cli.py:2169-2247`) gains lifecycle lines in its
existing timeline, sorted with the other entries.
`--decisions-only` continues to mean decisions only: lifecycle rows are
suppressed by it exactly as event deliveries already are
(`bobi/cli.py:2176`), because a lifecycle transition is not a manager decision.
The fold moves into a tested module and the CLI keeps only formatting; per F9
the five existing `TestEventsCommand` tests are the acceptance bar for that
move and must pass untouched.

**`health_summary`.** `LocalRuntime` replaces `"lifecycle": []`
(`bobi/webapp/runtime.py:839`) with the folded rows, capped at the same
`MAX_HEALTH_LIFECYCLE_EVENTS = 50` the hosted runtime uses
(`bobi/webapp/event_bus.py:66`), and its docstring's "there is no supervisor
here, so ... the lifecycle trail is empty" (`bobi/webapp/runtime.py:772-780`)
is corrected in the same commit.
This is the parity win: the payload key, the row shape, and the renderer are
now identical for local and hosted.

**One visible line in the local UI: a `last_transition` status segment.**
Without this, the local `bobi app` page would show nothing new, because
`renderBand` renders only `state`, `detail`, and `segments`
(`bobi/webapp/static/views/agent.js:218-240`) and has no lifecycle-row
renderer - so the payload would be populated and invisible.
`bobi/webapp/health.py::build_state` (`:182`) therefore gains one best-effort
segment built from the newest folded row:

```json
{"key": "last_transition", "label": "last transition", "kind": "text",
 "value": "restarted by operator", "note": "2026-08-06T04:10:22+00:00"}
```

This is deliberately the smallest surface that closes the gap. `segments` is
already documented as an ordered, best-effort list a client must render as
given (`bobi/webapp/runtime.py:281-287`), and `fmtSegment` already handles
`kind: "text"` (`bobi/webapp/static/views/agent.js:61-68`) - so this is **zero
new frontend code, zero new components, and no design-system decision**, which
is exactly why it does not reopen F10's non-goal. The status strip's job is
already "is this thing running"; why it last changed belongs there.

**The segment is omitted, never faked, when the journal is empty.** That is not
a preference; the suite already enforces it.
`test_webapp_health.py::test_never_run_agent_has_no_segments` asserts
`segments == []` for a fresh agent and says why in its own comment: "A fresh
agent's strip says STOPPED and nothing else. That is the whole truth about it -
inventing SINCE/EXIT/WAS UP would not be." A never-run agent has an empty
journal, so it keeps showing nothing, and that test passes unchanged.

**Blast radius, measured:** adding this segment breaks **exactly one** test -
`tests/test_webapp_health.py:188-189`, which asserts the exact ordered key list
`["uptime", "manager_pid", "live_runs", "last_activity"]`. It must be updated
in the same commit, and where `last_transition` lands in that list is a
display-order decision to make deliberately rather than by append.
The other three `segments == []` assertions do **not** break, checked
individually rather than assumed: the two endpoint/never-run tests run against
a bare `bobi_install` (a fresh isolated home with no prior run, hence an empty
journal), and `TestNormalize` exercises only the defaults `normalize` fills.

**`GET /api/agents/{name}/events`.** The event-queue read model, added beside
the existing `/runs` route (`bobi/webapp/server.py:190`) with a
`TeamRuntime.events()` abstract method implemented by both runtimes in the same
commit, per the ABC's own widening rule (`bobi/webapp/runtime.py:89-94`).

- `LocalRuntime.events()` reads the journal and folds it.
- `EventBusRuntime.events()` folds `buildInstanceDetail`'s `lifecycle` array
  (`event-server/worker/src/fleet.ts:542-556`) through the **same**
  `to_rows`, so the response shape cannot drift between deployments. No
  box-consulting command, no new admin verb, no `SUPERVISOR_VERSION` bump, and
  no TTL cache: it is one KV read per poll with no round-trip to the box, for
  the same reason `health_summary` needs none
  (`bobi/webapp/event_bus.py:525-532`).

Rejected name: `/api/agents/{name}/lifecycle`. It describes what ships today
more literally, but the row carries `kind` precisely so `event` and `decision`
rows can join without a contract change, and renaming a route later is the one
kind of churn a documented API should not need.

Response:

```json
{
  "rows": [
    {
      "kind": "lifecycle",
      "id": "9f2c1d84b7ae4f5390cc61d2a7e5b310",
      "event": "manager_restarted",
      "at": "2026-08-06T04:10:22.114233+00:00",
      "received_at": null,
      "correlation_id": "9f2c1d84b7ae4f5390cc61d2a7e5b310",
      "reason": "operator",
      "manager_pid": 4711,
      "restart_count": null,
      "origin": "manager",
      "detail": "manager restarted by operator (pid 4711)"
    }
  ],
  "counts": {"lifecycle": 12},
  "truncated": false,
  "retention_seconds": 172800
}
```

Field semantics: `at` is the recording box's own clock, ISO 8601 UTC;
`received_at` is server-receipt epoch **milliseconds** and is non-null only for
rows folded from the hosted trail, matching the existing convention
(`bobi/webapp/event_bus.py:545-548`); `id` is the group's `correlation_id` for
`kind: "lifecycle"` rows, and stays a separate field because the `event` and
`decision` kinds this shape reserves will not have one; `restart_count` is the
supervisor's window count and is null for local rows because a local agent has
no restart budget; `truncated` is true when the limit clipped rows;
`retention_seconds` reports the journal's configured bound so a client can say
how far back "all of it" goes - the `172800` above is option A's value and
moves with §9's decision, which is exactly why the field is in the response
rather than assumed by the client. `?limit=` defaults to 100, matching `runs.py`'s
`DEFAULT_LIMIT` (`bobi/webapp/runs.py:51`). Unknown fields are ignored by
consumers, per the protocol's compatibility promise.

**`detail` is generated, not stored**, and its template is fixed here so two
runtimes cannot word the same event differently:

```
detail = f"manager {VERB[event]}{by}{pid}"

VERB = {"manager_started": "started", "manager_stopped": "stopped",
        "manager_restarted": "restarted",
        "manager_start_failed": "start failed",
        "manager_stop_failed": "stop failed",
        "manager_restart_failed": "restart failed",
        "probe_failing": "probe failing",
        "probe_recovered": "probe recovered",
        "budget_exhausted": "restart budget exhausted"}

by  = f" by {reason}"  if reason and not event.endswith("_failed") else \
      f": {reason}"    if reason else ""
pid = f" (pid {manager_pid})" if manager_pid else ""
```

An event not in `VERB` renders as `event.replace("_", " ")` rather than raising
or inventing a verb, so a future supervisor emitting a name this version has
never seen degrades to something readable instead of a 500. Reason is joined
with "by" for a confirmed effect and with a colon for a failure, because "manager
stop failed by still_running" is not English and an operator reading a failure
row wants the cause, not an actor.

### 5.7 Supervisor changes

Two, both additive:

1. `correlation_id` threading (F2), as described in §5.4.
2. A `JournalObserver` added to the `_MultiObserver` fan-out
   (`bobi/supervisor/supervision.py:206-212`), writing every edge to the local
   journal with `origin: "supervisor"`. The fan-out is already per-observer
   fail-open, so a journal write error cannot reach telemetry, the alerter, or
   the restart state machine.

This is what makes the issue's "a broker failure must not erase the local
operational record" true: the durable copy is now written on the box, by the
process that decided, before any publish is attempted.

## 6. Mechanical inventories

Every call site below was produced by the printed command and **every hit is
classified** - the tables below hold one row per hit, not one row per file.
The previous revision's tables covered 25 of A's 28 hits and 11 of C's 14; the
missing rows are marked **(added rev 3)** so a reviewer can check the fix
rather than take it on faith.
No list here was written by hand.

### A. Local lifecycle entry points

```
$ rg -n -g '*.py' '\b(start_team|stop_team|restart_team|spawn_team|run_team_foreground)\s*\(' bobi/ | wc -l
28
```

| Hit | Classification |
|---|---|
| `bobi/service.py:340` `def spawn_team` | definition; unchanged |
| `bobi/service.py:420` `def start_team` | definition; unchanged (still only `launch_team`'s callee) |
| `bobi/service.py:428` `spawn_team(...)` | inside `start_team`; unchanged |
| `bobi/service.py:466` `start_team(...)` | inside `launch_team`; unchanged |
| `bobi/service.py:474` `def run_team_foreground` | definition; unchanged |
| `bobi/service.py:668` `def stop_team` | definition; **journal writer, gains `correlation_id` + `phase` kwargs** |
| `bobi/service.py:722` `def restart_team` | definition; **zero callers, out of scope** |
| `bobi/service.py:728` `stop_team(...)` | inside `restart_team`; out of scope |
| `bobi/service.py:729` `start_team(...)` | inside `restart_team`; out of scope |
| `bobi/webapp/runtime.py:119` `def start_team` | `TeamRuntime` ABC declaration; unchanged |
| `bobi/webapp/runtime.py:123` `def stop_team` | ABC declaration; unchanged |
| `bobi/webapp/runtime.py:127` `def restart_team` | ABC declaration; unchanged |
| `bobi/webapp/runtime.py:591` `def start_team` | **(added rev 3)** `LocalRuntime` impl; **`manager_start_failed` goes in its existing `except` arms** |
| `bobi/webapp/runtime.py:597` `service.spawn_team(...)` | the call those arms guard; unchanged itself |
| `bobi/webapp/runtime.py:606` `def stop_team` | **(added rev 3)** `LocalRuntime` impl; unchanged (the writers are the manager and `stop_team`) |
| `bobi/webapp/runtime.py:610` `service.stop_team(...)` | webapp stop; unchanged |
| `bobi/webapp/runtime.py:619` `def restart_team` | **(added rev 3)** `LocalRuntime` impl; **mints the correlation id for both halves** |
| `bobi/webapp/runtime.py:623` `service.stop_team(...)` | restart's stop half; **threads `correlation_id`, `phase="restart"`** |
| `bobi/webapp/runtime.py:628` `self.start_team(name)` | restart's start half; **threads the id via child env** |
| `bobi/webapp/server.py:227` `rt.start_team(name)` | HTTP handler; unchanged |
| `bobi/webapp/server.py:231` `rt.stop_team(name)` | HTTP handler; unchanged |
| `bobi/webapp/server.py:235` `rt.restart_team(name)` | HTTP handler; unchanged |
| `bobi/webapp/event_bus.py:375` `def start_team` | hosted lifecycle command; unchanged (the supervisor journals its own edges) |
| `bobi/webapp/event_bus.py:380` `def stop_team` | hosted; unchanged |
| `bobi/webapp/event_bus.py:385` `def restart_team` | hosted; unchanged |
| `bobi/cli.py:420` `run_team_foreground(...)` | foreground start; **confirmer thread covers it** (it runs inside `run_manager_from_config`) |
| `bobi/cli.py:422` `spawn_team(...)` | CLI start; **add `manager_start_failed` in the existing `except` arms** |
| `bobi/cli.py:1039` `stop_team(...)` | CLI stop; **pass `correlation_id` when invoked by `restart`**. Note F4: unreachable when a systemd unit is installed |

### B. `health_summary` implementations and the `lifecycle` key

```
$ rg -n -g '*.py' -g '*.js' 'health_summary|"lifecycle"|health\.lifecycle' bobi/ | wc -l
9
```

| Hit | Classification |
|---|---|
| `bobi/webapp/runtime.py:256` | ABC declaration; **docstring gains the new row fields** |
| `bobi/webapp/runtime.py:271` | ABC shape doc, the `lifecycle` key; **updated** |
| `bobi/webapp/runtime.py:772` | local impl; **docstring corrected** |
| `bobi/webapp/runtime.py:839` `"lifecycle": []` | **the defect; replaced with the folded rows** |
| `bobi/webapp/health.py:245` | `normalize` docstring; unchanged |
| `bobi/webapp/server.py:180` | route; unchanged |
| `bobi/webapp/event_bus.py:525` | hosted impl; unchanged |
| `bobi/webapp/event_bus.py:539` | hosted trail fold; **re-pointed at the shared `to_rows`** |
| `bobi/webapp/event_bus.py:575` | hosted payload key; unchanged |

No frontend hit. The local `bobi app` page renders `state`, `detail`, and
`segments` only (`bobi/webapp/static/views/agent.js:218-240`), which is why
§5.6 adds a segment rather than a renderer.

Correction to rev 2: it claimed the `lifecycle` mention in
`bobi/webapp/static/app.css:449-451` was dead CSS left over from the five-panel
page and listed "fix the stale CSS comment" as a deliverable. It is not stale.
Those three lines are a live comment explaining why
`.attention-head + .attention-row` (`:452`) keys the header gap off adjacency
instead of `:first-of-type` - because attention rows mix `<button>` and `<div>`
siblings. Nothing there is touched by this spec, and the deliverable is dropped
from §8.

### C. The events read path

```
$ rg -n -g '*.py' 'events-\*\.jsonl|decisions\.jsonl|_show_events|_log_event' bobi/ | wc -l
14
```

| Hit | Classification |
|---|---|
| `bobi/cli.py:2169` `def _show_events` | **gains lifecycle rows via `to_rows`** |
| `bobi/cli.py:2178` `events-*.jsonl` glob | unchanged |
| `bobi/cli.py:2218` `decisions.jsonl` | unchanged |
| `bobi/cli.py:2299` `_show_events(...)` | command body; unchanged |
| `bobi/doctor.py:638` | doctor's own glob; unchanged |
| `bobi/events/client.py:49` `def _log_event` | the append pattern §5.2 departs from; unchanged |
| `bobi/events/client.py:435` `_log_event(...)` | its one caller; unchanged |
| `bobi/inbox.py:194` | comment referencing `_log_event`; unchanged |
| `bobi/session.py:649` | import; unchanged |
| `bobi/session.py:650` | **(added rev 3)** `_log_event(...)` call; session lifecycle into the event log; unchanged |
| `bobi/session.py:723` | import; unchanged |
| `bobi/session.py:724` | **(added rev 3)** `_log_event(...)` call; unchanged |
| `bobi/session.py:773` | import; unchanged |
| `bobi/session.py:774` | **(added rev 3)** `_log_event(...)` call; unchanged |

### D. Webapp read-model routes

```
$ rg -c '@app\.(get|post)\("/api' bobi/webapp/server.py
23
```

23 routes; all unchanged. The new `GET /api/agents/{name}/events` is added
after `/api/agents/{name}/runs` (`bobi/webapp/server.py:190`), the route it is
the sibling of.

### E. Supervisor lifecycle emitters

```
$ rg -n -g '*.py' '\.lifecycle\(|_emit_lifecycle\(|_note_restart\(' bobi/ | wc -l
16
```

| Hit | Classification |
|---|---|
| `bobi/supervisor/supervision.py:206` | `_MultiObserver.lifecycle`; **`JournalObserver` joins this fan-out** |
| `bobi/supervisor/supervision.py:209` | the per-observer call inside it; unchanged |
| `bobi/supervisor/supervision.py:349` `budget_exhausted` | journaled; minted id (not operator-initiated) |
| `bobi/supervisor/supervision.py:380` `def _note_restart` | **gains `correlation_id` passthrough** |
| `bobi/supervisor/supervision.py:389` `manager_restarted` | the emit inside it; **carries the id** |
| `bobi/supervisor/supervision.py:441` operator restart | **passes the admin `command_id`** |
| `bobi/supervisor/supervision.py:453` operator stop | **passes the admin `command_id`** |
| `bobi/supervisor/supervision.py:462` operator start | **passes the admin `command_id`** |
| `bobi/supervisor/supervision.py:489` wedge restart | journaled; minted id, `reason` already carried |
| `bobi/supervisor/supervision.py:516` crash restart (charged) | journaled; minted id |
| `bobi/supervisor/supervision.py:528` crash restart (uncharged) | journaled; minted id |
| `bobi/supervisor/supervision.py:671` `run()` boot | journaled; minted id |
| `bobi/supervisor/supervision.py:694` `run()` teardown | journaled; minted id |
| `bobi/supervisor/telemetry.py:115` | observer entry point; unchanged |
| `bobi/supervisor/telemetry.py:186` `probe_failing` | journaled; minted id |
| `bobi/supervisor/telemetry.py:193` `probe_recovered` | journaled; minted id |
| `bobi/supervisor/telemetry.py:196` `_emit_lifecycle` | **`correlation_id` finally reaches it** |

## 7. Verification plan

Unit and integration, in `tests/`. The acceptance criteria map one-to-one.

**Standing requirement on every test below that touches the `deployment`
block:** clear `BOBI_INSTANCE`, `BOBI_AGENT`, `BOBI_FLEET`, and `FLY_APP_NAME`
with `monkeypatch.delenv(..., raising=False)`. Per F12 the host's own identity
otherwise leaks in, and the failure mode is the bad one: a test that passes for
the wrong reason on one host and fails on another. Two tests on `main` already
fail this way on a deployed host.

This lands as a shared `conftest.py` fixture so no future test has to remember,
and that carries **one named side effect** rather than a silent drive-by: the
two `test_supervisor_alerting.py` tests F12 identifies stop failing when the
suite is run on a deployed host. Verified as safe to include: no workflow under
`.github/workflows/` exports any of those variables, so CI behaviour is
unchanged either way and this only makes local runs on a deployed box honest.
Flagged because a reviewer seeing two unrelated tests change colour deserves to
know it was deliberate.

**`tests/test_manager_lifecycle_journal.py`** (new)
- append then read round-trips the canonical payload shape
- two concurrent appenders both land, neither line is torn
- **an append racing a retention prune is not lost** - the specific race §5.2's
  lock exists for, and the one this suite must fail without it: hold a writer
  at the lock while another crosses the entry cap, then assert both lines are
  in the surviving file
- **a wedged lock holder does not hang the writer**: with the lock held past
  `timeout`, `record()` returns within the bound and the caller is not blocked
  (F14). Without the bound this test hangs, which is the point
- retention prunes by age first and by count second, in that order (§5.2), and
  age is measured against the newest entry rather than wall clock: a journal
  whose newest entry is a week old is not emptied by reading it
- a read never mutates the file
- reconciliation appends `manager_stopped{reason: "reconciled"}` when the last
  start's generation is gone, and does not when it is live; two concurrent
  starts produce exactly one reconciled stop
- **pid reuse does not suppress reconciliation**: a recorded generation whose
  pid is alive but whose `start_token` differs reconciles as stopped; the same
  pid with the same token does not (F15)
- **a missing start token degrades visibly**: with the token unreadable, the
  reconciled entry carries `reason: "reconciled-unverified"`, not the
  confident reason
- fold rule 1: a correlated stop+start collapses to one `manager_restarted`
  carrying the start's `at`/`manager_pid` and the stop's `reason`
- fold rule 2: a lone stop with `phase: "restart"` collapses to
  `manager_restart_failed`; a lone stop without it stays `manager_stopped`
- fold rule 3: a `manager_restarted` + `manager_started` pair (the supervised
  restart) yields one `manager_restarted` row at the confirmed timestamp
- fold rule 4: a lone hosted `probe_failing` passes through unchanged
- **generation grouping**: a manager-written and a `stop_team`-written
  `manager_stopped` for one generation with *different* correlation ids
  collapse to one row taking `at` from the manager and `reason` from the
  operator (§5.5). Two stops for *different* generations stay two rows
- ordering: a mixed list orders on `received_at` where present and `at`
  otherwise, and a hosted-only list reproduces `listLifecycle`'s order
- `detail` renders each event in `VERB`, and an unknown event renders as its
  de-underscored name rather than raising
- a write to a read-only directory is logged and swallowed, not raised
- a malformed line is skipped, not fatal

**`tests/test_service_lifecycle_journal.py`** (new)
- the manager's `_cleanup` writes `manager_stopped` with `origin: "manager"`
  on SIGTERM, and does so **without any operator path running** - the systemd
  case of F4, exercised by signalling the process directly rather than by
  calling `stop_team`
- **`_cleanup` invoked twice writes one entry**, which is the real SIGTERM path
  (`bobi/service.py:575-580` reaches it through both `_handle_term` and
  `atexit`). Without the once-only flag this test finds two lines
- **a confirmer that finishes after shutdown begins writes nothing**: set the
  flag, then release the confirmer's waiters, and assert no `manager_started`
  lands after the generation's `manager_stopped`
- a clean exit with no signal at all still records the stop, via `atexit`
- `stop_team` writes `manager_stopped` on a confirmed exit, on a force kill,
  and on a stale pid
- `stop_team` writes `manager_stop_failed` on `still_running` and on
  `permission_denied`
- a `bobi agent stop` on a box where both writers run produces **one** folded
  row, not two
- the start confirmer writes `manager_started` once both waiters return, and
  `manager_start_failed` on either timeout
- `child_agent_env` strips an inherited `BOBI_LIFECYCLE_CORRELATION_ID`

**`tests/test_fsutil.py`** (extend)
- `file_lock(path)` with no timeout blocks exactly as it does today (the
  existing callers' behaviour is unchanged)
- `file_lock(path, timeout=...)` raises once the deadline passes and succeeds
  when the holder releases inside it

**`tests/test_supervision_operator.py`** (extend)
- an admin `restart` / `stop` / `start` puts its `command_id` on the edge
- `JournalObserver` writes every edge with `origin: "supervisor"`; a raising
  journal does not disturb telemetry, the alerter, or the restart decision

**`tests/test_webapp_health.py` / `test_webapp_event_bus.py`** (extend - these
are where `LocalRuntime` and `EventBusRuntime` are exercised today; there is no
`test_webapp_runtime.py`)
- `LocalRuntime.health_summary()["lifecycle"]` is populated and newest-first
- both runtimes' `events()` return byte-identical row shapes for equivalent
  input; the hosted fold sets `received_at` and the local fold sets it null
- `?limit=` clips and sets `truncated`
- the `last_transition` segment appears when the journal has entries and is
  absent (not faked) when it is empty, per `segments`' best-effort contract
- **update** `test_webapp_health.py:188-189`'s ordered key list to include
  `last_transition` at its chosen display position - the one test this spec
  knowingly breaks (§5.6), updated in the same commit rather than discovered by
  a red CI run

**`tests/test_webapp_server.py`** (extend)
- `GET /api/agents/{name}/events` returns 200 with the documented shape and
  404 for an unknown agent

**`tests/test_cli.py`** (extend, on top of `TestEventsCommand`)
- the five existing tests (F9) pass unchanged after the fold moves out of
  `_show_events` - the acceptance bar for that refactor
- `bobi agent events` interleaves lifecycle, event, and decision lines in
  timestamp order
- `--decisions-only` shows no lifecycle rows
- the CLI and `LocalRuntime.events()` return the same lifecycle history for the
  same journal, which is the acceptance criterion stated as a test

**Integration (`tests/integration/test_manager_lifecycle.py`, extend)**
- a real `launch_team` then `stop_team` writes exactly one confirmed
  `manager_started` and one `manager_stopped` to the real file
- a real manager killed with SIGTERM **directly** (no operator path) still
  writes its own `manager_stopped` - the end-to-end proof of the F4 fix, since
  this is what `systemctl stop` does
- a real manager killed with SIGKILL writes nothing, and the next start
  reconciles it
- the entries survive a simulated event-server outage, since nothing on the
  write path touches the bus

Brain-agnostic by the repo's own rule (`CLAUDE.md`, "Real-Claude e2e as
acceptance criteria"): this is process lifecycle and a read-model fold, so the
stub path proves it and no `[claude]` leg is warranted.

**Proof of work for the PR**: the real `run/state/lifecycle.jsonl` produced by
a real start/stop/restart cycle against an isolated `BOBI_HOME`, including one
cycle driven by a direct SIGTERM rather than by `bobi agent stop`, plus the
real `GET /api/agents/{name}/events` response body, both pasted into the PR
description.

## 8. Implementation plan

Ordered so each step is independently green.

1. `fsutil.file_lock` gains `timeout`, with its tests. No behaviour change for
   existing callers.
2. `bobi/manager_lifecycle.py`: schema, generation identity, `record()`,
   `read()`, locking, retention, reconcile, `fold()`, `to_rows()`, and its unit
   tests. No callers yet.
3. The manager's own stop entry in `_cleanup`, and the start confirmer in
   `run_manager_from_config`. This is the pair that makes the systemd case work
   (F4), so it lands before anything that depends on stops being recorded.
4. `service.stop_team` writes stop and stop-failure entries; `child_agent_env`
   strips the correlation var.
5. Restart correlation on both unmanaged local paths; start-failure entries at
   the two operator entry points.
6. Supervisor: `correlation_id` threading and `JournalObserver`.
7. Reads: `LocalRuntime.health_summary`, the `last_transition` segment in
   `build_state`, `_show_events`, `TeamRuntime.events()` on both runtimes, the
   route.
8. Docs: `docs/LIFECYCLE_JOURNAL.md`, `docs/ADMIN_PROTOCOL.md`,
   `docs/AGENT_STATE.md`, `docs/RUNS_VIEW.md`.

## 9. Risks and open questions

**Open decision for the requester: the local retention bound.**
This is escalated deliberately rather than decided here, because the two
options serve different people and the spec has no standing to pick.

| Option | `LIFECYCLE_RETENTION_S` | What it buys | What it costs |
|---|---|---|---|
| A | `48 * 60 * 60` | one bound across both trails; the local journal and the Worker's `LIFECYCLE_TTL_S` (`event-server/worker/src/fleet.ts:120`) agree, so "how far back does this go" has one answer on every deployment | a Friday-evening stop is gone by Monday morning, which is the requester's own framing of the gap this issue opens with |
| B | longer (e.g. `7 * 24 * 60 * 60`) | a weekend incident is still there on Monday | the two trails disagree, and that disagreement has to be documented at both ends so nobody reads a hosted 48h window as the whole history |

The symmetry argument for A is weaker than it looks, and that is worth stating
plainly rather than letting "consistency" carry the decision: the Worker's bound
is a KV `expirationTtl` (`event-server/worker/src/fleet.ts:174`) - storage the
platform reclaims on its own schedule - while the local journal is a bounded
file on local disk that this spec prunes itself. Matching them is a choice, not
a constraint.
Two coupled facts a decision should weigh: `MAX_LIFECYCLE_ENTRIES = 500`
already caps the file's size independently of time, so option B costs disk
only in proportion to how many transitions actually happened; and §5.3's
generation check gets more load-bearing as the window grows, since pid reuse
becomes likelier the longer an unreconciled start sits.

**Recommendation: option B at 7 days**, because the issue's stated pain is
"downtime I cannot explain on Monday" and 500 entries is a small file at any
horizon. But this is a recommendation, not a decision, and implementation
should not start on this constant until the requester picks. Either way it is
one constant and one comment.

Other risks:

- **The manager now writes to disk during shutdown.** `_cleanup` runs on the
  SIGTERM path, so the journal write sits between the signal and the exit. It
  is bounded by §5.2's lock timeout, fail-open, and ordered after nothing that
  matters - but it is a new I/O on a path that previously only unlinked a pid
  file, and it should be reviewed as such.
- **`_cleanup` does not run on SIGKILL, and cannot.** That is the whole reason
  reconciliation exists (§5.3). The consequence a reader should expect: an
  OOM-killed manager shows no stop until the next start, and then a
  `reconciled` one whose `at` is the moment the gap was noticed, not the moment
  the process died. The projection says so rather than implying precision.
- **The confirmer thread is a new thread in the manager.** It is a daemon,
  bounded by the existing 30s waiter budget, and fail-open. It cannot delay
  shutdown and cannot fail a start.
- **Multi-writer correctness rests on an advisory `flock`.** `fsutil.file_lock`
  is `fcntl.flock`, so it binds only writers that take it. Every writer in this
  design does, and the suite pins the append-versus-prune race directly. A
  future writer that appends without the lock would reintroduce the
  lost-line bug; the module's docstring says so at the write function.
- **A supervised box now records each edge twice** (journal + bus). That is the
  point (durability), and the fold collapses them on read. It does cost one
  small file write per edge.
- **Open question for the requester:** should `manager_start_failed` also fire
  when a manager boots successfully and then dies during startup, before the
  confirmer's waiters return? As specced, that produces a
  `manager_start_failed` from the timeout, which reads as "start failed" rather
  than "started then crashed". Distinguishing them needs the requester to watch
  the child, which no local path does today (F3). Recommendation: ship as
  specced and revisit if the reason string proves misleading in practice.

## 10. Review record

This spec has been through three review passes; the findings below are folded
into the design above, not appended to it.

**Second-opinion tooling is unavailable in this environment and is not being
claimed.** `codex exec` returns `401 Unauthorized` and `aichat` has no
configured gateway (no `OPENROUTER_API_KEY` / `OPENAI_API_KEY` in the
environment). No cross-model pass was run at any revision. Revision 3's inputs
are an independent read-only verifier with full repo access, and direct
re-derivation against the tree; both are named as what they are.

### First pass

| # | Finding | Resolution |
|---|---|---|
| B1 | The retention prune rewrites the file via `atomic_write_text`, which replaces the inode (`bobi/fsutil.py:29-38`). An appender holding an fd on the old inode would silently lose its line. | §5.2 now takes `fsutil.file_lock` on **every** write, not just the prune. A test pins the race. |
| B2 | Two processes could each append a reconciled `manager_stopped` for the same dead generation. | §5.3 scopes reconciliation to `manager_started` writes and `stop_team`'s stale branch, both under B1's lock. |
| M1 | The original fold rule keyed on `phase: "restart"`, but the start half is written by the manager, which cannot know why it was spawned. The rule was unimplementable. | §5.4 propagates only the id; §5.5 rule 1 derives the restart from the stop+start pair, and `phase` now earns its place solely on rule 2. |
| M2 | A supervised restart produces `manager_restarted` **and** `manager_started` under one id, and the original rules gave no precedence. | §5.5's field-precedence table plus rule 3's rank. |
| M3 | Nothing would have changed in the local `bobi app` UI: the payload would be populated and invisible. | §5.6 adds one `last_transition` entry to the existing `segments` list - zero new frontend code - and §4 says plainly what an operator sees where. |
| m1 | `bobi/lifecycle.py` collides with `LIFECYCLE_EVENTS` (`bobi/events/subscriptions.py:64`), which means *session* lifecycle. | Module renamed `bobi/manager_lifecycle.py`; §4 records why. |
| m2 | `--decisions-only` must not start showing lifecycle rows. | Stated in §5.6, tested in §7. |
| m3 | A journal write failure must never fail a start or stop. | Fail-open stated in §5.2 and tested. |
| m4 | Does `--fresh` wipe the journal? | Verified it does not (`bobi/service.py:145-155`); stated in §5.2. |
| m5 | Does the hosted `events()` need the spend cache's TTL treatment? | No - one KV read, no box round-trip. Stated in §5.6. |
| m6 | `/events` returns only one `kind` today; why not `/lifecycle`? | Rejected alternative recorded in §5.6 with the reason. |

### Second pass

The spec's premises were re-checked **by running them** rather than re-reading
them. Four became findings the design carries, and re-reviewing the revised
text produced two more.

| # | Finding | Resolution |
|---|---|---|
| V1 | Is widening `TeamRuntime` with `events()` actually safe? | Ran the grep: exactly two subclasses, both in-tree. Recorded as F11. |
| V2 | Does `file_lock` survive the prune's inode swap? | It locks a **companion `.lock` file**, precisely so it does (`bobi/fsutil.py:165-167`). §5.2 records that and the new `.lock` sibling. |
| V3 | Does `resolve_deployment_identity` really resolve for an unsupervised local agent? | Ran it with the fleet env cleared: `fleet: "default"`, instance from the run-root basename. §5.1 cites a run. |
| V4 | How many tests does the new `segments` entry break? | Exactly one - `test_webapp_health.py:188-189`. The three other `segments == []` assertions were each checked and do not break. |
| V5 | The host's identity leaks into tests via the same function §5.1 reuses. | New F12, with a standing `delenv` requirement on §7's tests and a shared fixture, whose one side effect is named. |
| V6 | A `try` *inside* `with file_lock(...)` is fail-CLOSED, because the acquisition itself raises first on a read-only state dir. | §5.2 shows the wrong shape explicitly and states the guard goes outside. |

### Third pass

An independent read-only verifier with full repo access re-checked the spec
against `origin/main` at `5d2cf04` and returned `revise_before_gate_1`. It
confirmed F11, V4, V6, and the CI-env claim as exact, and confirmed F12's
finding by executing the two named tests.

R1-R10 are its defects and D1-D4 are director rulings. **S1-S3 are defects in
this pass's own fixes**, found by re-reading the revised §5.3 rather than by
re-reading the original: R1's fix moved the stop writer into a shutdown path
that runs twice, which is the sort of thing only a second look at the new text
catches. They are listed here rather than quietly corrected because a review
record that only ever finds faults in earlier revisions is not a review record.

| # | Finding | Resolution |
|---|---|---|
| R1 | **The confirmed-effect model was wrong under systemd.** `bobi agent stop` and `restart` return at `bobi/cli.py:1031-1034,1074-1089` without ever reaching `stop_team`, the spec's only stop writer, so a systemd box would journal no stops at all. Rev 2's line 385 claimed the confirmer covered systemd. | The model changed, not a caveat added. §5.3 makes the **manager itself** the primary stop writer, in the `_cleanup`/SIGTERM path it already has (`bobi/service.py:562-582`), which every service manager and every `kill` goes through. `stop_team` stays for the outcomes only it can see. Recorded as F4; §7 tests the direct-SIGTERM case explicitly. |
| R2 | **launchd appeared zero times in 903 lines**, though MOD-352 records that `bobi agent stop\|restart` fight it and that a KeepAlive respawn makes "process exited" different from "service stopped". | Two answers. Scoped **out** in §4 with the reason stated and MOD-352/#925 named: there is no launchd integration in this repo to confirm effects through (grep is clean). And §5.3 now states the boundary generally - the journal records process generations, never service-manager intent - which is what makes the design correct under launchd, systemd, and container restart policies alike without modelling any of them. F13 supplies the evidence: no unit ships here, so no `Restart=` is knowable. |
| R3 | **F9 was false.** `tests/test_cli.py:414` is `TestEventsCommand`, five tests on the display path. The `rg '_show_events' tests/` that produced the claim was a false negative - the tests drive the click command. | F9 rewritten as the opposite finding: the command **is** covered, and those five tests are the acceptance bar for moving the fold into a module. §7 says so. |
| R4 | **§5.5's ordering parity claim was wrong.** `listLifecycle` sorts on `received_at` (`event-server/worker/src/fleet.ts:185`), not `generated_at`, and local rows have no `received_at` at all. | §5.5 now specifies `received_at` when present, `at` otherwise, and states what that means on hosted-only, local-only, and merged lists - including that a merged ordering is approximate, rather than claiming a parity the data cannot support. |
| R5 | **The `supervisor/identity.py:60-91` citation was stale, and the alternative §5.1 rejected had already landed in #980.** | §5.1 rewritten: `bobi/identity.py:67-98` is now a plain core module, so the function-local import and the rejected-alternative paragraph are both gone. The design got simpler. |
| R6 | **Inventories A (25 of 28 hits) and C (11 of 14) were incomplete**, while the PR body claimed every inventory was mechanical and every hit classified. | Both completed from re-run commands, with the added rows marked. §6's preamble now states one row per hit, not per file, and the PR body's claim is corrected rather than left standing. |
| R7 | **Line drift across `cli.py`, `paths.py`, `inbox.py`, and more**, from the `b5388bb` base. | The branch was rebased onto `5d2cf04` and every citation re-derived from a printed command. |
| R8 | **P2, half-fixed in rev 2:** the wording moved from "lock timeout" to "lock failure" but `file_lock` still blocks unboundedly, so a wedged holder could hang a start or a stop. | New F14 and a bounded acquisition in §5.2: an optional `timeout` on the shared helper, defaulting to today's behaviour, with the journal passing 2s and treating expiry as a skipped write. §7 pins it with a test that hangs without the bound. |
| R9 | **F12's own citation was invented** - `supervisor/identity.py:70-91` is wrong at both commits, though the finding it supports is real and was proven by execution. | Finding kept, citation replaced with the printed grep over `bobi/identity.py`, and the error named in F12 so the correction is auditable. |
| R10 | **Rev 2 claimed `app.css:449-451` was dead CSS from the five-panel page** and listed fixing it as a deliverable. | False, found while re-deriving inventory B: those lines are a live comment explaining an adjacency selector. The claim is corrected in §6 B and the deliverable dropped from §8. |
| D1 | Director ruling: fold **pid reuse** in now, do not defer to the gate. The reconciler is the repo's first caller to check a pid up to a retention window old (F15). | §5.3 requires both pid liveness and a matching start token, §5.1 defines the generation, and the retention coupling is stated where it will be re-read: the check gets more load-bearing as the window grows. |
| D2 | Director ruling: **retention is escalated**, present both options and recommend. | §9 leads with the decision table, states plainly that matching the Worker's TTL is a choice rather than a constraint, and recommends 7 days without settling it. |
| D3 | Director ruling: the "Linear access is unavailable" claim is **false** and has been corrected four times. | Deleted. §11 now records the actual state. |
| D4 | Director ruling: specify four cheap unspecified items rather than deferring - prune order, the `detail` template, which `origin` each supervisor edge carries, and the id format. | §5.2 (prune order, age before count, with the reason), §5.6 (the `detail` template plus its unknown-event fallback), §5.5 (`origin` is who wrote it, never who asked, enumerated across all eleven supervisor emit sites), §5.4 (`uuid4().hex`, and why hosted ids stay opaque). |
| S1 | Found while re-reading R1's own fix: **`_cleanup` runs twice on the SIGTERM path**, once from `_handle_term` and once from `atexit` (`bobi/service.py:575-580`). Every step in it is idempotent today, so the double call is invisible - but a journal append is not, and the fix for R1 would have recorded two `manager_stopped` lines for one termination. | §5.3 adds a once-only flag set under the journal's lock, and §7 tests the double invocation directly. §5.5's generation grouping is named as the second line of defence rather than relied on as the first. |
| S2 | Same re-read: the start confirmer is a daemon thread that can still be waiting when SIGTERM lands, so it could append `manager_started` **after** its own generation's `manager_stopped`. | The confirmer checks S1's flag before writing and stays silent if shutdown has begun - correct, because a generation that never reached readiness did not start. Tested in §7. |
| S3 | Same re-read: reading the start token at shutdown means forking `ps` inside a signal handler on macOS (§5.1). | The manager reads and caches its own token at boot, so the shutdown path only formats a held value. The reconciler stays the only caller that reads another process's token, and it runs at start time. |

## 11. Tracking

No tracking action is owed, and the previous revisions were wrong to say
otherwise.

The issue asks for a backlink to the existing Bobi management-UI ticket. That
link already exists and is correct:

- the Linear mirror of #933 is **MOD-359** ("Add agent start, stop, and restart
  lifecycle events to the event queue"), state **In Progress**, whose
  description already carries `GitHub: moda-labs/bobi-agent#933`;
- its parent is **MOD-261** ("Single Agent details"), which **is** the
  management-UI epic the issue asked to connect to - the same epic that holds
  the single-agent-view units MOD-335 through MOD-342.

GitHub issue #933's body already records this. Revisions 1 and 2 of this spec,
and the PR description, claimed "Linear access is unavailable in this
environment"; that was false, and the claim is removed here rather than
softened.

The console-side render remains MOD-261's work (§4, out of scope); this spec's
deliverable is the API it reads.
