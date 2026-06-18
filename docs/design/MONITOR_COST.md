# Monitor Cost — Cheap Detection, Escalate-on-Hit

Status: **design, ready for Linear epic.** Driving problem: event-shaped
needs ("when I get an email from X, do Y") are materialized by `modastack
setup` as **polling monitors**, and the default flavor spawns a full
default-model (Opus) session on **every tick whether or not anything
happened**. Two monitors polling MCP sources every 5m ≈ ~576 fresh Opus
sessions/day, almost all concluding "nothing new."

This epic makes a poll cost ~$0 when idle, and moves the expensive
reasoning to the already-running manager session — only on a real hit.

---

## How it works today (grounded, file:line)

Two ingestion paths feed the same destination (an event topic the manager
session subscribes to over WebSocket). Their cost profiles are opposite.

**Push path (cheap).** Only **GitHub / Linear / Slack** are true webhooks
(`event-server/src/index.ts:135,157,163`). A delivered webhook is posted
into the *already-running* manager session's inbox (`inbox.py:176,213`) —
**no new LLM session is spawned**. (Exception: rule-gated reactor
auto-dispatch, `reactor.py:125`, deliberately launches a workflow agent.)

**Poll path (expensive for one flavor).** Everything else — including
**email/Gmail** — is poll-only. There is no Gmail/email webhook surface
anywhere (no `/webhooks/gmail`, no Pub/Sub, no IMAP; no email adapter in
`events/adapters.py`). The `MonitorScheduler` thread ticks every 30s
(`scheduler.py:107`) and runs due monitors. Flavor dispatch
(`scheduler.py:319-332`):

| Flavor | LLM per tick? | Path |
|---|---|---|
| `notify: true` | No | in-process Condition |
| `command:` | No | `subprocess.run`, 60s (`scheduler.py:386-428`) |
| native `check:` | No | Python fn in `*_checks.py` (`scheduler.py:373-384`) |
| **description-only** | **Yes** | `_spawn_check` → `agents launch --wait` → `_run_agent_supervised` |

The description-only flavor spawns a **full supervised session**:
`max_turns=200`, `CHECK_TIMEOUT=600s`, **up to 2 attempts**, on a **fresh**
session each tick (deliberate fresh `run_key`, `subagent.py:1090-1093`),
on the CLI's default model — **Opus, for this user**.

**Why these get generated.** `modastack setup` routes *every* `autonomous`
behavior to a polling monitor (`setup/authoring.py:193-211`). An
event-shaped cadence ("when I get an email") **fails to parse as an
interval and falls back to 15m** (`setup/authoring.py:30-39`), and because
email has no webhook adapter it's emitted as a **description-only** monitor
(the expensive flavor). There is **no webhook-vs-poll chooser** and **no
model selection** anywhere — `ClaudeAgentOptions` never sets `model`
(`session.py:198-205`, `subagent.py:229-245`), so checks inherit the same
model as the manager.

**Already shipped (reuse):** Multi-model **Phase 1** (`6ecd066`) added
cost attribution — `SessionEntry.{model,provider,model_usage}` and
`modastack costs --by model|role|session`. Spend is already attributable;
this epic makes it cheap. **Phase 2** (per-role model selection) is
designed in `MULTI_MODEL.md` but **not built** — Part A is its minimal
slice.

---

## Goal

A recurring poll costs ~$0 when nothing changed. Expensive reasoning runs
**only on a real hit, inside the already-running manager session**, not in
a fresh Opus session per tick. Two combined changes:

- **Part A** — put checks on a cheap model (minimal slice of Phase 2).
- **Part B** — cheap deterministic detection; escalate-on-hit. *(Built first.)*

---

## Part B — Cheap detector + escalate-on-hit *(first)*

The architecture's own detect → reconcile → publish path already does the
hard part for free: `_command_conditions` parses a detector's JSON into
conditions keyed by `id`, and `_reconcile` (`scheduler.py:341-359`) fires
an event **only for keys that weren't active last tick**. So a detector
that emits the *current* set of items (e.g. unread message IDs) yields
exact "new-on-arrival" semantics with no per-detector last-seen
bookkeeping and **no LLM**.

**Decision (locked): native `check: venn_poll` runner.** *(over a raw
`command:` shell string or a setup-generation-only change — chosen for
reusability across teams and to keep the Venn-pull logic in one tested
place rather than in generated shell strings.)*

1. **`venn_poll` native check runner** — a framework-level Python check
   (registered in `CHECKS`, invoked via the existing `monitor.check` path,
   `scheduler.py:373-384`) that runs the Venn CLI pull for a configured
   service + tool (e.g. `tools execute -s work-gmail -t list_messages`),
   normalizes the result to a list of `{id, ...}` items, and returns them
   as conditions. Pure subprocess — **$0 LLM per poll**. Dedup/new-item
   semantics come for free from `_reconcile`. Params (service, tool, query,
   id field) come from the monitor record's `check_args`. The published
   event wakes the **live manager session** (cheap inbox delivery) to do
   the real work.
2. **Two-tier semantic gate** (for "about X" needs that genuinely need an
   LLM to judge relevance): `venn_poll` still does the cheap mechanical
   pull; when a semantic filter is required, the recurring *gate* runs on
   the cheap model (Part A) with a small `max_turns` cap and returns only a
   verdict; the expensive reasoning/action runs in the persistent manager
   session when the verdict fires an event. Detect cheap → act expensive,
   only on real hits.
3. **Cap the check budget**: lower the check path's `max_turns` from 200 →
   ~8 (new param on `_run_agent_supervised`, `subagent.py:199,239`, passed
   from `run_check_blocking`, `subagent.py:1188`) so a single poll can't
   balloon into a 200-turn run.

Step 1 (`venn_poll`) is fully independent of Part A and ships first; step 2
depends on Part A's cheap model.

---

## Part A — Model selection on the check path (minimal Phase 2 slice)

1. **Config schema** (`config.py:153-172`): add `ModelSpec`
   (`harness: claude-sdk|gateway`, `model`, optional `gateway`); parse
   `defaults.model` and per-role `roles.<role>.model`. Secrets via existing
   `${ENV_VAR}`.
2. **Resolver**: `resolve_model(cfg, role)` by precedence — launch flag >
   `.modastack/roles/<role>` override > team `roles.<role>.model` >
   `defaults.model` > built-in `claude_code`. Unchanged when unconfigured.
3. **Thread into the SDK** — set `model=` (merge `env=` for gateway) in
   `session.py:198-205`, `subagent.py:229-245` (`_run_agent_supervised`),
   and `run_check_blocking` (`subagent.py:1047-1138`, resolve the
   **`monitor`** role's model from `Config` — the check subprocess reloads
   `Config`, so no flag threading). Add `--model` to `agents launch`.
4. **Gateway = local-SLM option, no SDK change**: a `kind: gateway`
   connection (reuses Phase 1 registry) injects
   `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_MODEL` via `env`,
   letting a local SLM (LiteLLM/Ollama-compat) drive checks.
5. **Ship a cheap default**: set `roles.monitor.model: claude-haiku-4-5`
   in generated/installed packs (eng-team, marketing, setup output) so
   every existing and new install stops using Opus for checks. Built-in
   fallback stays default only when explicitly unconfigured.

---

## Tests (repro-first, per CLAUDE.md)

- **Repro/integration**: assert today a monitor check spawns a session on
  the default model; the fix flips it to (a) the cheap model via Phase-1
  cost attribution (`SessionEntry.model`), and (b) a `venn_poll` detector
  that finds new items with **zero** LLM sessions.
- **Unit**: config parses `defaults.model`/`roles.<role>.model`; resolver
  precedence; gateway→`env` mapping; `venn_poll` normalizes pull output to
  `{id}` conditions and `_reconcile` fires only on new IDs (mocked Venn CLI).

## Verification

`modastack costs --by model --by role --since 7d` before/after on a real
install to confirm the spend collapse.

## Defaults & decisions

- Cheap model = **Haiku 4.5** (Sonnet if filters are nuanced).
- Check `max_turns` cap = **8**.
- Built-in default model unchanged when unconfigured (no behavior change
  for installs that don't opt in); generated packs opt in via step A5.

## Out of scope (separate follow-ups)

- **Lever D** — full setup-routing rework ("subscribe instead of poll when
  a webhook adapter exists"). Parts A5 + B1 already cover most of D's value
  for newly generated monitors.
- **Lever C** — native Gmail push via Pub/Sub `watch` + event-server
  adapter. Eliminates email polling entirely; higher infra/onboarding lift.
