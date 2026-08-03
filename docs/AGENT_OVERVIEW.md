# Team composition, read-only — and what the cache didn't spend

Two reads behind the agent page's identity header. `GET .../overview` answers
*what is this agent?* — the read-only mirror of a team's composition. And the
spend payload gains a `script_cache` block: what the cached-script monitor
runner **did not** spend.

They ship together because they are the header's two hover cards, but they are
independent folds: `bobi/webapp/overview.py` and `bobi/webapp/savings.py`.

## `GET /api/agents/{name}/overview`

```json
{"name": "content-review",
 "description": "Watches the inbox and files tickets.",
 "roles": [{"name": "director", "description": "Dispatches work."}],
 "chat": {"service": "slack", "channels": ["C123"]},
 "services": [{"name": "slack", "events": true, "required": false}],
 "automations": {"monitors": 5, "paused_monitors": 1, "workflows": 2},
 "brain": {"kind": "claude", "model": "", "effort": "", "max_turns": 0,
           "gateway": false},
 "spend_cap": {"value": 50, "is_default": true},
 "entry_role": "director"}
```

Read-only is the posture, not a limitation: composition is edited in setup, and
this surface exists so nobody has to open setup — or `agent.yaml` — to remember
what a team is. Every value comes from the **installed package image**
(`run/package/`), never a source directory. The runtime runs the image, so the
image is the truth.

**Defaults are made explicit where absence still means something.** An unset
brain still runs one, so `kind` falls back to the framework default. An unset
`spend_cap` still enforces one, so `value` carries the effective number and
`is_default` says where it came from. But an unset `model` stays empty — "the
provider's default" is not a name this layer gets to guess.

**A gateway is reported as a fact, never as a URL.** `brain.base_url` can carry
a key, so the payload says only that one is configured.

**A team in trouble still gets an answer.** An `agent.yaml` that will not parse
costs the fields it would have carried, not the response — the page whose job is
telling you the agent is broken must not 500 because the agent is broken.

### Counting automations is not counting `agent.yaml`

What actually runs is the merge of framework defaults, the package's
`monitors.yaml`, and the team's own entries, so a count taken from any one layer
is wrong. This one goes through `MonitorRegistry` — specifically
`effective_monitors()` filtered by `projects_for()`, the same pair the scheduler
uses.

That pairing is load-bearing, because **`enabled: false` means two different
things** in that registry:

- on a **framework default**, it pauses the monitor;
- on a **team's own entry**, it is an *opt-out* of an inherited default. The
  entry never enters the project list at all; it removes the global from this
  team.

Counting `all_monitors()` by its `enabled` flag would therefore report a default
the team switched off as scheduled work. `paused_monitors` is the remainder —
everything in the registry that is not live for this team, whichever way it was
switched off.

## `script_cache` savings, inside the spend payload

A description-only monitor pays for an LLM call every interval to re-reason the
same check. A `script_cache` monitor pays once — the agent works the check out
and emits a shell script — and every later tick runs that script for nothing.
The runner records both halves per monitor in
`run/state/scripts/<name>.state.json`.

```json
{"monitors": 3, "cached_runs": 412, "agent_runs": 7,
 "agent_cost_usd": 2.10, "estimated_savings_usd": 61.80, "priced_monitors": 2}
```

The estimate is **each monitor's own arithmetic**: that monitor's cached ticks ×
what that monitor's agent ticks actually cost on average. Not a fleet-wide
blend, which would price a cheap monitor's savings with an expensive one's bill.

Two things it deliberately does not do:

**No modelled price for a monitor that never paid one.** With no agent tick on
record — which is the normal case under subscription auth, where a tick records
$0 — there is no basis, so nothing is estimated. `priced_monitors` is the
honesty dial: when it is `0`, the saving reads `0` because nothing *could* be
priced, not because nothing was saved. A caller showing dollars should show
`cached_runs` beside them; the count is always true.

**No summing with recorded spend.** These are counterfactual dollars. They sit
in their own block, beside a bill and never inside one — the same separation
`estimated_cost_usd` already keeps from `total_cost_usd`.

## Compatibility

`TeamRuntime.overview()` is **not** an `@abstractmethod`, for the same reason
`runs()` is not: an out-of-tree subclass in the private deploy repo implements
this ABC, and marking the method abstract here would break its CI the moment
this merges. It becomes abstract once the hosted runtime implements it. The base
raises `TeamLifecycleError` meanwhile.

`script_cache` is additive to a payload whose other keys are byte-stable for
older consumers.
