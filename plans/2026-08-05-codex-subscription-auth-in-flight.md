# Subscription auth for CLI tools, in-flight: a worker that 401s on `codex` can be authorized over Slack without a restart

> **Status:** Draft - awaiting Gate 1 approval from Zach. No implementation until approved.
> **Tracking issue:** moda-labs/bobi-agent#958 · **Created:** 2026-08-05
>
> Every claim in **Problem** was verified first-hand inside a live eng-team worker container on 2026-08-05.
> Raw evidence is in [Appendix A](#appendix-a--verification-transcript).
> The design is post-two-rounds-of-adversarial-review; see [Notes](#notes) for what each round changed and what is still owed.

## Purpose

`codex` is the team's only cross-model adversarial reviewer.
It returns 401 in worker containers, and there is no fallback model, so the cross-model pass is not degraded but **impossible**.
Every review this fleet runs is therefore single-model, and each one records an opinion it still owes.

The subscription-login ceremony that fixes this already exists and already knows how to log `codex` in.
It is wired to the **brain** only, at **container start** only, and writes to a directory that **does not survive a restart** on a Claude-brained team.
This plan removes those three constraints.

## Problem

### 1. The 401 is "no credential at all", not an expired or rejected one

```
ERROR: unexpected status 401 Unauthorized: Missing bearer or basic authentication in header,
url: https://api.openai.com/v1/responses, request id: req_a74fcecafe3c426889629cc0394c5870
```

`codex exec` exits **1**. `codex` reads auth from `$CODEX_HOME`, defaulting to `$HOME/.codex`, and in this container:

- `~/.codex/auth.json` is **absent**. `~/.codex/config.toml` is **absent**.
- `OPENAI_API_KEY` is **unset**, so codex has nothing to fall back to even if it wanted to (and per #479 it does not fall back for `/v1/responses`).
- `OPENROUTER_API_KEY` and `AICHAT_PLATFORM` are unset, so `aichat` is not a fallback either.

So the issue's framing is confirmed on the outcome and sharpened on the cause: this is not a token to refresh, it is a login that never happened.

### 2. The ceremony already supports codex, but only when codex is the *brain*

`bobi/auth_bootstrap.py` carries a complete `codex` login spec:

```python
"codex": SubscriptionLogin(
    kind="codex",
    login_cmd=("codex", "login", "--device-auth"),
    creds_relpath=(".codex", "auth.json"),
    shadow_env="OPENAI_API_KEY",
    flow="device_poll",
    url_re=re.compile(r"https://auth\.openai\.com/codex/device\S*"),
    code_re=re.compile(r"\b([A-Z0-9]{4}-[A-Z0-9]{5})\b"),
)
```

**It works.** Driving `codex login --device-auth` under a pty in this container printed the URL and one-time code, and both regexes matched verbatim against codex-cli 0.144.5 ([Appendix A](#appendix-a--verification-transcript)).

It is unreachable for us because the spec is selected from the brain and nothing else:

- `run_bootstrap` calls `set_process_brain(cfg.brain_kind)`, then `_active_spec()` reads `BOBI_BRAIN`.
- eng-team declares `brain: {kind: claude}`, so `_active_spec()` returns the **claude** spec, always.
- `run_bootstrap` has no parameter for "which CLI do I want to authenticate".

`bobi agent <name> login-bootstrap` on this team drives `claude auth login`, finds the brain credentials already present, and exits.
There is no code path that reaches the codex spec on a Claude-brained team.

### 3. It is genuinely start-time-only

`run_bootstrap` / `login-bootstrap` has exactly **one** automated caller in the tree:

```sh
# docker/docker-entrypoint.sh:566
if [ "${BOBI_AUTH:-api_key}" = "subscription" ] \
   && [ ! -f "${BRAIN_CRED_DIR}/${BRAIN_CRED_FILE}" ]; then
  as_app bobi agent "${AGENT_NAME}" login-bootstrap
fi
```

Container entrypoint, brain credential file, boot.
Nothing in the manager, the runtime, the supervisor, or any monitor re-invokes it.
The issue's "as far as we know, start-time only" is confirmed: the CLI command exists so a human on `fly ssh console` can run it, but no running agent ever does.

### 4. On a Claude-brained team, codex credentials have nowhere durable to live

The entrypoint puts codex credentials on the volume **only when codex is the brain**:

```sh
# docker/docker-entrypoint.sh:484-529  (guarded by ENTRYPOINT_ENGINE = codex)
BRAIN_CRED_DIR="${DATA_DIR}/codex"
BRAIN_HOME_LINK="${HOME}/.codex"
ln -s "${BRAIN_CRED_DIR}" "${BRAIN_HOME_LINK}"
```

In this container: `/data/codex` **does not exist**, and `~/.codex` is a real directory on the container's ephemeral overlay (`none 7.8G` on `/`), not on the volume (`/dev/vdc` on `/data`).
So authenticating codex today would buy exactly one container lifetime.
That turns a once-per-machine ceremony into a once-per-deploy ceremony, which is not shippable.

### 5. In subscription mode the entrypoint deliberately gives auxiliary codex nothing

```sh
# docker/docker-entrypoint.sh:536-560
if [ "${BOBI_AUTH:-api_key}" != "subscription" ]; then
  ... materialize_codex_api_key_auth "${HOME}/.codex" ...
elif [ -n "${OPENAI_API_KEY:-}" ]; then
  log "Subscription mode: leaving OPENAI_API_KEY out of Codex auth materialization"
fi
# and, in subscription mode, deletes any api-key-shaped auth.json
```

This is correct and stays: subscription OAuth must remain authoritative and an ambient API key must not silently outrank it (#522).
The consequence is that on a subscription-auth Claude-brained team, the auxiliary codex tool is guaranteed to have no credential by design, with no path to acquire one.
**That gap is this plan's whole subject.**

### 6. Nothing fails loudly, because for this team codex is not preflighted at all

The eng-team pack ships `tools/codex.md` telling agents:

> The CLI is baked into the eng-team image and preflighted by `agent.yaml` `requires:` (installed + authed).

Verified false. The composed `package/agent.yaml` `requires:` list contains `gh`, `gstack`, `moda-skills`, and **no codex entry**.
`tools/codex.md` is a hand-authored team-pack file (provenance `moda-eng-team@1.16.0`), not the `bobi/tool_library/codex/` guide, so the tool-library `success:` check - which *does* exercise `codex exec` and would have caught this - is never wired in.
The binary is baked into the base image (`/usr/local/bin/codex`, owned by uid 1001, dated 2026-07-16), so `command -v codex` succeeds and everything downstream assumes auth.

The first thing that notices is a review agent, mid-run, at the moment the opinion was needed.

### 7. A worker can already send the brain's OAuth flow to any destination it likes

Found while designing this, verified in this container, and fixed here because it is the same attack surface:

```
$ bobi agent eng-team login-bootstrap --help
Options:
  --channel TEXT   Private chat channel or gateway conversation ref to post
```

`login-bootstrap` is registered on the **agent** command group (`cli.py:3557-3561`) with no privilege gate, and the entrypoint runs it via `as_app` (= `gosu "${APP_USER}"`, line 412), the same uid workers run as.
`_resolve_login_channel` accepts any `source:scope:type:id` ref, and `_post_login_message` provisions grants for it and sends.
So any worker - including one processing a fork diff, an issue body, or a webhook payload - can run `bobi agent <name> login-bootstrap --channel slack:…:dm:<attacker>` and send the **brain's** login URL to an arbitrary recipient.
Its only current guard is `credentials_exist()` at `cli.py:613`, which is True on eng-team purely because `~/.claude -> /data/claude` happens to hold credentials. That is an accident, not a boundary.

### What the framing gets wrong, and why it matters

- **"Worker containers", plural, is not the topology.** `bobi/subagent.py` spawns subagents as local detached subprocesses (`_launch_detached`, line 872, called from `launch_agent`, line 1025) inside the agent's own Fly machine. Every worker shares one `$HOME` and one `~/.codex`. **One login in the container authenticates every worker in it, including ones already running.** That is what makes in-flight repair cheap rather than a per-worker credential-distribution problem.
- **"Paste code" is inverted for codex.** Claude's flow is `paste_back`: the human pastes a code back into Slack. Codex's flow is `device_poll`: bobi posts a URL **and** a one-time code, the human enters the code on OpenAI's page, and the CLI polls until authorized. Nothing is ever pasted into Slack.
  This is strictly simpler, and it means the codex path **cannot** hit #860 (Slack top-level channel messages being dropped), because it never waits for an inbound message.
- **Presence of a credential file proves nothing, and neither does `codex login status`.** `codex login status` is local-only (17 ms, no network) and returns **exit 0** for a syntactically-valid but entirely bogus API key ([Appendix A](#appendix-a--verification-transcript)). Any design that decides "is codex authenticated?" from the filesystem or from `login status` will report success for a credential that 401s. This constraint drives [D](#d-the-outcome-is-decided-by-a-real-probe-never-by-a-file).

## Solution

Six changes. Each is small; the ordering is the order they matter.

### A. Let the ceremony target a CLI tool instead of the brain

`run_bootstrap(..., target="codex")` resolves `_SPECS["codex"]` directly.
Everything downstream (pty spawn, scrape, chat post, credential check) takes the **resolved spec as a required argument** rather than re-deriving it from process state.

Required, not defaulted, because the ambient default is actively wrong here: `bobi agent <name> ...` pins `BOBI_BRAIN` to the team's brain before any command body runs (`cli.py:174` `_bind_agent_runtime` -> `_pin_team_brain` -> `set_process_brain_from_config`).
So inside a codex `tool-login`, `_active_spec()` returns the **claude** spec.
A missed threading site would not crash, it would silently check `~/.claude/.credentials.json`, find the brain's credentials, and report codex as authenticated.
`cli.py:613` is exactly that shape today, so the new command must not be modelled on it.

### B. Give `auth.json` a durable home - on exactly the teams that need it

Symlink **the single credential file**, not the directory:

```
~/.codex/auth.json  ->  ${DATA_DIR}/codex/auth.json
```

**Verified: codex writes through the symlink.** A real `codex login` against a symlinked `auth.json` left the symlink intact and landed `-rw-------` content on the target ([Appendix A](#appendix-a--verification-transcript)).

`~/.codex` stays on the image, holding what it holds today: `state_*.sqlite`, `logs_*.sqlite`, `sessions/`, `shell_snapshots/`, `skills/` (2.2 MB of live codex state).
That directory is image-owned and the team build mutates it by name (`agent.yaml` `build.run`, and the gstack `requires:` check reads it), so it must keep tracking the image.

**The block runs only when `BOBI_AUTH=subscription` AND the brain is not codex.** Both guards are load-bearing, and each was proven necessary by reproducing the failure it prevents ([Appendix A](#appendix-a--verification-transcript)):

- **Not codex-brained.** On a codex brain the entrypoint already links `~/.codex -> ${DATA_DIR}/codex` (lines 89-91, 526-530), so `~/.codex/auth.json` and `${codex_vol}/auth.json` are *the same path*. An earlier draft ran this block for every engine, and the self-heal branch then did `mv -n a a` (silent no-op), `rm -f a` (**destroys the live OAuth credential**), `ln -sfnT a a` (**ELOOP**). Reproduced end to end: credential gone, path unreadable, and unrepairable on later boots because the self-heal predicate no longer matches. Every codex-brained subscription machine would have lost its brain credential on first boot after the roll.
- **Subscription only.** In `api_key` mode `materialize_codex_api_key_auth "${HOME}/.codex"` writes through any symlink, which would put a plaintext `OPENAI_API_KEY` on a snapshotted volume, every boot, surviving a later switch to subscription mode. Reproduced. Gating on subscription mode keeps #522's behaviour byte-identical: the key stays on the ephemeral overlay and dies with the container.

So the block runs on exactly one population - subscription-mode, non-codex-brained teams - which is precisely the set that has the bug.

Two details:

- **Ownership.** The entrypoint runs as root until `exec gosu` (line 630), and the recursive `chown` of `$DATA_DIR` is gated on the `.bobi-owned` stamp that every already-deployed machine already has. `mkdir -p "${DATA_DIR}/codex"` alone lands `root:root`, and the app user can then write neither the credential nor the lock file. The block chowns explicitly, mirroring lines 485-486.
- **Self-healing, local-wins.** If a future codex ever replaces the symlink with a regular file, that file is by definition the *newer* credential, so it wins: `mv -f` onto the volume, then re-link. An earlier draft used `mv -n`, which silently kept the stale volume copy and deleted the fresh one.

`CODEX_HOME` stays unset, so codex and `auth_bootstrap.credentials_path()` both resolve through `$HOME` and agree, and `credentials_path()` needs no change (which is why this plan does not touch #861's lines).
A dangling `~/.codex/auth.json` (volume file not yet created) behaves exactly like a missing file for both `Path.is_file()` and codex, so the pre-login state is correct by construction.

### C. Make the trigger in-flight, and bound it honestly

New command `bobi agent <name> tool-login codex`, callable by any worker in the container.

`--timeout` is the budget for the **whole command**, not just the device poll: the probe, the scrape (`url_timeout`, 120 s today), the channel work, and the poll all draw from it.
Default **300 s**, comfortably under the harness's 600 s Bash ceiling, and the recovery recipe wraps the call in a real `timeout 420` **and** tells the agent to raise the Bash tool's own `timeout` parameter, whose default is 120 s.
An earlier draft defaulted to 600 s, stacked the 120 s scrape on top, and expressed the Bash timeout as a shell comment - which sets nothing and would have made the graceful-timeout path unreachable.

### D. The outcome is decided by a real probe, never by a file

`run_bootstrap` today ends with `ok = credentials_exist(home)` - a `Path.is_file()` check that is only meaningful because the pre-check guaranteed absence.
An in-flight caller has already proven presence does not imply validity, so that post-check is vacuous exactly when it matters: a revoked token on disk, an absent operator, and the command reports **success**.

So each `_SPECS` entry gains a **probe**, and the probe is not a bare exit code:

```python
@dataclass(frozen=True)
class AuthProbe:
    argv: tuple[str, ...]      # codex: ("codex","exec","-s","read-only","--skip-git-repo-check","reply OK")
    timeout: float             # bounded; drawn from the command budget, never unbounded
    unauth_re: re.Pattern      # the 401 signature, e.g. r"401 Unauthorized: Missing bearer"
```

Three outcomes, because two is a lie:

| Probe result | Meaning |
|---|---|
| exit 0 | authenticated |
| non-zero **and** `unauth_re` matches | unauthenticated - a login will help |
| non-zero, no match, or probe timeout | **inconclusive**: 429, 5xx, upstream outage, sandbox error - a login will not help |

Classifying every non-zero as "unauthenticated" would page an operator and burn a device code during an OpenAI outage on a container whose credential is fine.
`codex login status` is not usable for any of this: local-only, and exit 0 for a bogus key (Problem, last bullet).

The probe decides the exit code, and it also replaces the credential-fingerprint compare-and-swap an earlier draft used for concurrency: a caller that takes the lock probes first and returns immediately if someone else fixed things.
One mechanism, both jobs, and it answers the only question anyone actually has.
`--force` therefore no longer means "trust me, skip the check". It means "skip the *presence* pre-check"; the probe still gates entry and exit.

`"reply OK"` is fixed argv, never caller-controlled, so no review content reaches the probe.

### E. Single-flight that never blocks the herd

Eight workers share a container (`max_concurrent_agents: 8`) and can all 401 within seconds.

`bobi/fsutil.py::file_lock` gains `blocking: bool = True`; the new call site passes `blocking=False` (`LOCK_EX | LOCK_NB`).
Existing callers are unchanged by the default, so this stays one lock mechanism rather than a hand-rolled `fcntl` beside it.
It has to be a real parameter: `file_lock` today is an unconditional blocking `LOCK_EX` with no deadline, so queueing would park seven workers for the winner's full timeout inside a call each was told is bounded.
A loser returns **exit 3** immediately and degrades, which is the correct answer - its review is not the one that will benefit.

The same exit 3 carries the cooldown: a stamp beside the lock suppresses starting *another* ceremony within one timeout of the last, so a wedged agent looping on 401s neither spams the login channel nor burns device codes.
The cooldown is evaluated **after** the probe, so a healthy container always gets exit 0 rather than a spurious exit 3.

The control that stops an operator being pointed at a dead code is the **cancellation message** (below), not the interval.

### F. Close the arbitrary-destination hole on both commands

`tool-login` has no `--channel`; the destination is always `$BOBI_LOGIN_CHANNEL`.
And `--channel` is dropped from the agent-group `login-bootstrap` registration too, because Problem §7 shows it is worker-reachable today and hands an attacker-chosen destination the *brain's* OAuth flow.
The boot path does not need the flag (it reads `$BOBI_LOGIN_CHANNEL`), and an operator on `fly ssh console` can still set the env var for a one-off.

### Why blocking, and not fail-cheap-and-retry

The issue names this as the design decision, so it is stated plainly.

Fail-cheap (post the request, abandon the pass, let a later run pick it up) does not satisfy the issue's "Done means", which requires the cross-model pass to *actually run* without a container restart.
It also loses the run's context: the reviewer is holding the diff and the findings at exactly the moment it needs the second opinion.

Blocking forever is the opposite failure: an absent operator wedges a worker for its whole turn budget.

The bounded block gets both. **One** worker waits, for at most 300 s, inside a single Bash call, so it costs one turn (the fleet's anti-polling rule). Every other worker returns in milliseconds with exit 3. A timeout converts a missing operator into today's known-acceptable degradation rather than a hang.

**The load-bearing assumption is that an operator sees the post within minutes**, and the design must earn that rather than assume it: see [Notification and provenance](#notification-and-provenance) and Q2.

### Alternatives considered

- **Bake `codex login` into the image build.** Rejected: #479's fix for the API-key path, but OAuth tokens are per-account and rotate; baking one into a published image is a credential leak and cannot be re-minted without a rebuild.
- **Whole-directory `~/.codex` symlink, or `CODEX_HOME=/data/codex`.** Rejected; see B. Both move image-owned live state onto the volume to make one file durable.
- **A boot-time warm login for codex** (parallel to the brain's). Not in scope; see Q1.
- **Add a hard `requires:` gate for codex so it fails at deploy.** Rejected for now; see [Out of scope](#out-of-scope).
- **Fix the fallback instead: configure `aichat`/OpenRouter.** A real option for "cross-model opinion exists" but not for "subscription auth reaches CLI tools", which is what was asked. Worth its own ticket if Zach wants belt and braces.

## Scope

### In scope

1. `bobi/auth_bootstrap.py`: required-spec threading, `run_bootstrap(target=...)`, `AuthProbe`, result object, send-path pre-flight, orphan reaping, tool-target messaging.
2. `bobi/cli.py`: `tool-login` command (incl. `--status`); `--channel` dropped from the agent-group `login-bootstrap`.
3. `bobi/fsutil.py`: `file_lock(..., blocking=True)`. Default preserves every existing caller.
4. Non-blocking single-flight + cooldown, both surfacing as exit 3.
5. `docker/codex-auth.sh` (new, sourceable) + its call from `docker/docker-entrypoint.sh`: durable `auth.json` symlink on subscription non-codex-brain teams, app-user owned, self-healing; subscription sweep made non-destructive and target-resolving.
6. `bobi/tool_library/codex/guide.md` + `docs/TOOL_LIBRARY.md`: the recovery contract.
7. **A companion PR in `moda-agents`** carrying the same recovery contract into `moda-eng-team`'s `tools/codex.md` and deleting its false preflight claim. Not optional; see [Delivery](#delivery-the-bobi-agent-pr-alone-does-not-close-958).
8. Tests per [Verification](#verification), including a fake-codex stub so the ceremony is testable without an operator.

### Out of scope

- **API-key auth for codex.** #522/#479 own it, and B is now gated so their behaviour is byte-identical.
- **A hard `requires:` preflight gate for codex.** It would block dispatch fleet-wide *before* any codex login exists, converting a degraded review into a dead fleet. Loudness comes instead from the probe, a greppable log line, and the Slack post. Revisit once the fleet has been through one ceremony.
- **Anything on the brain target's auth logic.** #863, #868, #901, #861, #860 keep their fixes. (Dropping `login-bootstrap --channel` is a CLI-surface fix, not an auth-logic one, and touches none of their lines.)
- **Claude `paste_back` for a tool target.** No tool needs it; the codex tool is `device_poll`.
- **Configuring `aichat`/OpenRouter as a second fallback.**
- **Gateway teams.** The gateway guard stays as-is for every target; see below.
- **A Claude-brained *gateway* team reaching `api.openai.com` through #522's api-key path.** Real, pre-existing, and not created or worsened here. Worth its own ticket.

## Technical approach

### `auth_bootstrap`: resolve the spec once, pass it down

```python
BRAIN_TARGET = "brain"

def resolve_spec(target: str, cfg: Config) -> SubscriptionLogin:
    """The login spec for *target*: the team's brain, or a named CLI tool."""
    if target == BRAIN_TARGET:
        set_process_brain(cfg.brain_kind)     # unchanged: brain path seeds BOBI_BRAIN
        return _active_spec()
    try:
        return _SPECS[target]
    except KeyError:
        raise RuntimeError(
            f"no subscription login is defined for '{target}' "
            f"(known: {', '.join(sorted(_SPECS))})."
        ) from None
```

`credentials_path(home, spec)`, `credentials_exist(home, spec)`, `_spawn_login(home, spec)` and `_scrape_login(fd, timeout, spec)` all take the spec **required**.
Existing brain callers pass `_active_spec()` explicitly. A slightly larger diff than defaulting, and that is the point: a missed site becomes a `TypeError` under test, not a silently wrong credential check.

### Guards

| Guard | Today | After |
|---|---|---|
| `cfg.brain_is_gateway` | refuses every login | **unchanged**: refuses every target |
| `spec.shadow_env` set | raises | **mode-aware** (below) |
| `credentials_exist()` pre-check | returns True | unchanged unless `--force`; the **probe** is the real gate (D) |

**Gateway, unchanged.** `validate_auth_mode` already fatals on gateway + subscription (entrypoint:155-156), so gateway teams are always `api_key`, where the shadow-env guard refuses anyway. No team shape loses a capability it could otherwise have had, and leaving the predicate alone removes this plan's only adjacency with #863. An earlier draft narrowed the guard to `target == "brain"`; that is dropped, because on a gateway team it would let an agent run `codex login --device-auth` straight against `auth.openai.com` and route model traffic around the audit and spend boundary, with an operator's authorization on it.

**Shadow env, mode-aware,** because the current message cannot be followed. `OPENAI_API_KEY` is legal on a Claude-brained subscription team (`validate_auth_mode` only forbids the *brain's* shadow key, entrypoint:146) and `bobi/env.py` propagates it to every child, but subscription mode refuses to materialize codex api-key auth and sweeps any api-key file. So "use the #522 API-key path" points at a path this mode has already disabled:

- **`BOBI_AUTH=api_key`**: refuse, and name #522's path, which is genuinely available there.
- **`BOBI_AUTH=subscription`**: proceed, and **log a warning** naming the key. `_spawn_login` already does `env.pop(spec.shadow_env)` so the ambient key cannot contaminate the ceremony, and per #479 codex does not read `OPENAI_API_KEY` for `/v1/responses`, so it cannot outrank the OAuth credential at runtime either (verified: with an ambient key and no `auth.json`, codex sends **no bearer at all**). Refusing here would make codex permanently unauthenticatable on such a team with no working instruction.

The real shadowing vector is the *file*, not the env var, which is what the sweep below is for.

### `tool-login`: the wire shape

```
bobi agent <name> tool-login <tool> [--force] [--timeout SECONDS]
bobi agent <name> tool-login <tool> --status
```

| Field | Meaning |
|---|---|
| `<tool>` | a key in `_SPECS` other than the brain sentinel; today only `codex` |
| `--force` | skip the "credentials already present" presence pre-check. The probe still gates entry and exit |
| `--timeout` | total budget for the whole command: probe, lock, scrape, post, poll. Default 300 |
| `--status` | print local state - probe result, whether a ceremony is in flight, the in-flight run/machine id, cooldown remaining - and exit. No ceremony, no post |

There is deliberately **no `--channel`** (F).

| Exit | Meaning | Caller does |
|---|---|---|
| 0 | probe passes: codex authenticates | run the cross-model pass |
| 2 | ceremony ran, probe still shows unauthenticated | degrade to same-model, record the owed cross-model opinion |
| 3 | a ceremony is in flight, or one ran within the cooldown | retry `codex` once (it may have just been fixed), else degrade as for 2 |
| 1 | misconfigured, unusable, or **probe inconclusive**: no login channel, unknown tool, gateway team, `OPENAI_API_KEY` in api_key mode, channel send failed, 429/5xx/outage | report the error; do not retry, and do not page an operator |

`run_bootstrap` today returns a bare `bool` and swallows `TimeoutExpired` into the same `credentials_exist()` result, so it cannot express this. It returns a small result object (`ok`, `reason`), and the CLI maps `reason` to the exit code.

### Ordering inside the ceremony

Order is a correctness property here, and the current code has it wrong for this use: `run_bootstrap` spawns the pty, scrapes, and only *then* touches the channel. If the post fails, a device code has already been minted and burned, and the URL and code existed only in a subprocess's stderr. Nobody is told a login is pending.

1. **take the lock, non-blocking** - cheapest possible shedding, burns nothing; on failure exit 3
2. **probe** - if it passes, exit 0 without a ceremony; if inconclusive, exit 1
3. **cooldown** - if a ceremony ran within the interval, exit 3
4. **channel pre-flight**: register, then **post a real heads-up message** ("codex login starting on machine X, run Y - code follows"). This is what actually validates the destination. Registration alone does not: `_register_login_channel` provisions workspace-level credentials and grants and never touches the channel id, so `channel_not_found`, `not_in_channel`, and a missing scope surface only on send. On failure exit 1, having burned nothing. It also gives the operator a few seconds' warning before the code appears.
5. **reap** an orphaned poller from a previous killed run (pidfile beside the lock)
6. **spawn** the pty, scrape URL + code
7. **post** the code, with provenance
8. **poll** until the remaining budget is spent
9. **reconcile** the symlink (B), **probe again**, post the outcome or the cancellation, exit 0 / 2

Step 5 exists because `_spawn_login` uses `start_new_session=True` and the `finally` that terminates the child does not run if the caller is SIGKILLed; without reaping, an orphaned `codex login` keeps polling OpenAI's token endpoint, one per killed run. Termination also escalates `terminate` -> `wait` -> `kill`, which it does not today.

A scrape timeout at step 6 still exits 1 after a heads-up has been posted; the heads-up says a code is coming, so the operator sees the failure rather than silence.

### Notification and provenance

The whole case for blocking rests on an operator responding inside 300 s, and today `_post_login_message` posts a plain, anonymous message.

The tool-target post carries an **@-mention** (`$BOBI_LOGIN_MENTION`, see Q2), the agent name, the Fly machine id, the run id, and a deadline:

```
🔐 *codex subscription login needed* @<on-call>
`eng-team` on machine `d8d0926a026d28` (run `wf-issue-lifecycle-…`) hit a codex 401.

Open this link, sign in, then enter the one-time code:
https://auth.openai.com/codex/device
Code: `9S1A-79NNG`

_Polling until 00:20:14 UTC. Verify with:_ `bobi agent eng-team tool-login codex --status`
```

`device_poll` binds whichever OpenAI account enters the code, and codex's own banner warns *"Continue only if you started this login in Codex. If a website or another person gave you this code, cancel."*
A bot that routinely hands operators codes trains them past that warning, so an attacker who can post in the channel - or who can prompt-inject an agent - could post a lookalike carrying a code from a login **they** started.

Provenance in the message text is **not** sufficient against that: anything printed in Slack can be copied into a lookalike. That is why the message points at `--status`, which reads the local lock, pidfile, and cooldown stamp on the machine. A local state file cannot be forged from Slack, so it gives the operator a real out-of-band check rather than a string that looks official. The residual risk is Q3.

On timeout the poller is killed and a **cancellation message** supersedes the post, so a live-looking code never sits in Slack attached to a dead poller. (Authorizing a cancelled code binds nothing locally - the token is never collected - so the cost is a wasted operator action, not a compromise.) The existing boot-worded results (`"✅ … starting up"`, `"❌ … Fallback: fly ssh console"`) are wrong mid-run and get tool-target wording naming `tool-login` as the retry.

### Durable `auth.json`: `docker/codex-auth.sh`

Extracted into its own sourceable script so it can be tested without docker and without running the boot (`docker-entrypoint.sh` has no sourcing guard and interleaves top-level execution with its definitions). The entrypoint sources it and calls the function; a test asserts both the call and its position.

```sh
# docker/codex-auth.sh
# Durable codex OAuth for teams where codex is a TOOL, not the brain.
# Deliberately narrow: see plans/2026-08-05-codex-subscription-auth-in-flight.md §B.
codex_auth_link_volume() {           # $1 data_dir  $2 home  $3 owner  $4 engine  $5 auth_mode
  [ "$5" = "subscription" ] || return 0     # api_key: #522 owns it, key stays ephemeral
  [ "$4" != "codex" ]       || return 0     # codex brain: ~/.codex is ALREADY the volume link
  codex_vol="$1/codex"; codex_auth="$2/.codex/auth.json"
  mkdir -p "${codex_vol}" "$2/.codex"
  chown "$3:$3" "${codex_vol}"; chmod 700 "${codex_vol}"
  if [ -f "${codex_auth}" ] && [ ! -L "${codex_auth}" ]; then
    mv -f "${codex_auth}" "${codex_vol}/auth.json"    # local file is the NEWER credential
  fi
  ln -sfnT "${codex_vol}/auth.json" "${codex_auth}"   # -f: an existing path must not abort the boot
  chown -h "$3:$3" "${codex_auth}"
}
```

The subscription sweep is fixed in the same script.
Two bugs, both from the credential now living behind a link:

- It renames the **symlink**, not the credential, so the api-key file survives on the volume and is re-linked every boot - the sweep would neutralize nothing while appearing to work. It must resolve the target (`readlink -f`) and act there.
- `rm -f` on durable state is too sharp. `codex_auth_uses_api_key` keys on `data.get("OPENAI_API_KEY") and not data.get("tokens")`, and `tokens` is codex-internal and unversioned; a codex release that renames or nests it would make every boot read valid OAuth as an api-key file. Today that costs one container lifetime; behind the link it would force a device-login ceremony every boot forever, which Problem §4 already rejects as not shippable. So the sweep **renames the resolved target** to `${codex_vol}/auth.json.api-key.bak` and logs. The `.bak` is durable and observable, and the blast radius of a wrong predicate drops from destruction to a stale file. A dangling link then reads as "no credential", which is correct.

### The recovery contract agents follow

```bash
# codex 401 -> ask the operator to authorize, then retry once.
# Set the Bash tool's `timeout` parameter to 480000 (ms) for this call: the default
# is 120s and would kill the ceremony. `timeout 420` enforces it regardless.
codex exec -s read-only "$PROMPT" < /dev/null || {
  timeout 420 bobi agent "$BOBI_AGENT_NAME" tool-login codex --force --timeout 300
  case $? in
    0)   codex exec -s read-only "$PROMPT" < /dev/null ;;           # authorized: the pass runs
    3)   codex exec -s read-only "$PROMPT" < /dev/null \
           || echo "cross-model opinion still owed: login already in flight" ;;
    2)   echo "cross-model opinion still owed: operator did not authorize in time" ;;
    *)   echo "codex login unavailable, see the error above" ;;
  esac
}
```

One Bash call for the login, one turn, no polling.

### Delivery: the bobi-agent PR alone does not close #958

`bobi/tool_library/codex/guide.md` reaches **zero** eng-team agents.
`package/agent.yaml` has no `tool_library:` key, so nothing from `bobi/tool_library/` is expanded for this team; and `docs/TOOL_LIBRARY.md:119` records that a team's own `tools/<name>.md` wins anyway, which eng-team ships.

So the agent-facing half is delivered exclusively by the `moda-agents` companion PR (In scope #7).
Without it the bobi-agent PR ships a working CLI command that nothing invokes, every agent keeps reading the false "preflighted (installed + authed)" claim, and #958's "Done means" is unmet.
The library guide is still worth updating for teams that do use `tool_library: [codex]`; it is just not the delivery channel here.

Verification 30 therefore drives the flow **through an agent following the guide**, not a hand-typed `tool-login` - a hand-typed run would pass green while the agent-facing path stayed unreachable.

## Relevant files

### Existing, verified 2026-08-05

- `bobi/auth_bootstrap.py` - `_SPECS` (codex spec 82-92, verified working), `credentials_path` 104, `credentials_exist` 110, `_spawn_login` 124 (`env.pop(shadow_env)` 132, `start_new_session=True` 138), `_scrape_login` 145, `_register_login_channel` 248 (workspace-level only, never touches the channel id), `_post_login_message` 288, `_ensure_discord_paste_back_ready` 299 (the validate-before-spawn precedent), `run_bootstrap` 470, `url_timeout=120` 474, gateway guard 494, shadow-env guard 515, spawn 531, scrape 558, post 563, `proc.wait` 573, terminate 577-583, `ok = credentials_exist(home)` 585.
- `bobi/cli.py` - `_bind_agent_runtime` 168-175 / `_pin_team_brain` 178-200 (pins `BOBI_BRAIN` on every `bobi agent` call); `login-bootstrap` 596-626 incl. the bare `credentials_exist()` at 613; agent-group registration 3557-3561 (**this is what makes `--channel` worker-reachable**); top-level pop list 3574.
- `docker/docker-entrypoint.sh` - codex-brain cred dir 89-91, `as_app` 412 (`gosu "${APP_USER}"`), skills + dir symlink 480-530 (incl. the `chown` at 485-486), `materialize_codex_api_key_auth` 317-332, `codex_auth_uses_api_key` 334-353, api-key materialization 536-549, subscription sweep 551-561, brain bootstrap 566-570, `exec gosu` 630, `.bobi-owned` chown gate 368-377. No sourcing guard; top-level execution is interleaved with definitions.
- `bobi/fsutil.py:162` - `file_lock`; an unconditional blocking `LOCK_EX` (line 180) with no deadline, which is why E adds a parameter rather than queueing.
- `bobi/subagent.py:872` (`_launch_detached`, from `launch_agent` 1025) - subagents are local detached subprocesses; one container, one `$HOME`.
- `bobi/env.py` - propagates `OPENAI_API_KEY` into child agents.
- `bobi/tool_library/codex/tool.yaml` - the `success:` check that would have caught this, unused by this team; its `timeout=8` on the probe call is the precedent for bounding ours.
- `docs/TOOL_LIBRARY.md:119` - a team's own `tools/<name>.md` wins over the library guide.
- `tests/test_auth_bootstrap.py` - `test_run_bootstrap_skips_when_creds_present` 546, `..._refuses_with_api_key_set` 562, `..._refuses_gateway_brain` 568.
- `tests/integration/test_container_image.py` - **the only place the entrypoint is exercised**, `pytestmark = pytest.mark.docker` plus a `requires_docker` skipif, excluded from normal CI.

> **Correction.** An earlier draft cited `tests/test_gitops_c22.py` and `test_entrypoint_materializes_codex_api_key_auth_file` as existing entrypoint coverage, taken from PR #522's description. Neither exists in the tree today. The verification plan is rebuilt on harnesses that do exist, plus one new one.

### New

- `docker/codex-auth.sh` - the sourceable extraction above.
- `tests/test_codex_auth_sh.py` - **new, non-docker.** Sources `docker/codex-auth.sh` (which is safe to source, unlike the entrypoint) and drives the function against a temp `HOME`/`DATA_DIR`, with a stub `chown` on `PATH` so a non-root pytest can assert the ownership *call* rather than tautologically asserting its own uid. Runs on every PR.
- `tests/test_auth_bootstrap.py` additions.
- `tests/fixtures/fake-codex/` - a stub `codex` that plays back the recorded 0.144.5 device banner, writes a real-shaped `auth.json` when a trigger file appears, and fails the probe with the real 401 text until then. This is what makes the ceremony testable without an operator.
- `tests/fixtures/codex-device-login.txt`, `tests/fixtures/codex-auth-oauth.json` - the recorded banner, and a real `codex login` output captured for the sweep test rather than hand-written.
- `plans/2026-08-05-codex-subscription-auth-in-flight.md` - this file.

## Verification

No frontend, so no QA phase.

**Target resolution and guards** (`tests/test_auth_bootstrap.py`)

1. `run_bootstrap(target="codex")` on a Claude-brained team drives `codex login --device-auth` and checks `~/.codex/auth.json`, with `BOBI_BRAIN` still pinned to `claude` throughout.
2. Every spec-taking helper rejects a missing spec (`TypeError`).
3. `target="brain"` is byte-for-byte today's behaviour.
4. The gateway guard fires for **both** targets on a gateway team.
5. `OPENAI_API_KEY` + `api_key` -> exit 1 naming #522. `OPENAI_API_KEY` + `subscription` -> proceeds, warning logged.
6. Unknown tool -> exit 1 listing known targets.
7. `tool-login` has no `--channel`; **`login-bootstrap` no longer has one either** on the agent group, and the boot path still works from `$BOBI_LOGIN_CHANNEL`.

**Probe** (fake-codex stub)

8. Probe passes at entry -> exit 0, no pty, no post.
9. Ceremony runs, trigger appears -> exit **0**.
10. Ceremony runs, no trigger, probe emits the real 401 text -> exit **2**.
11. **The regression that motivates D:** a pre-existing `auth.json` whose probe 401s, `--force`, operator absent -> exit **2**, not 0, with the file on disk throughout.
12. Probe exits non-zero with a 429/5xx body -> exit **1**, no ceremony, no post, no page.
13. Probe hangs -> bounded by its own timeout, classified inconclusive -> exit 1.
14. Corroborating real-CLI check: a terminated `codex login --device-auth` leaves no `auth.json` (Appendix A), so presence is not accidentally load-bearing.

**Concurrency**

15. Two concurrent calls -> one pty, one post; the loser exits **3** in well under a second.
16. Eight concurrent calls -> one ceremony, seven exit 3, total wall clock bounded by the winner alone.
17. Cooldown is evaluated after the probe: healthy container inside the interval -> exit **0**, not 3.
18. Cooldown suppresses a ceremony -> exit **3**, no spawn, no post.
19. Lock holder SIGKILLed -> next caller acquires, reaps the orphan by pidfile, runs fresh.
20. `file_lock`'s existing callers are unaffected by the new parameter's default.

**Budget, lifecycle, ordering**

21. Total wall clock of a timed-out run is under `--timeout`, scrape budget drawn from it, not stacked.
22. Timeout kills the poller (terminate -> wait -> kill) and posts the cancellation.
23. A send failure at the **heads-up** post exits 1 with no pty spawned and no code minted.
24. Registration succeeds but `chat.postMessage` fails (`channel_not_found`) -> caught at step 4, still no code minted.
25. `--status` reports in-flight/cooldown/probe state and never spawns or posts.

**Scraper**

26. `_scrape_login` against the recorded 0.144.5 banner yields the URL and a well-formed code.

**`docker/codex-auth.sh`** (`tests/test_codex_auth_sh.py`, non-docker, runs on every PR)

27. Subscription + non-codex brain: creates `${DATA_DIR}/codex` with the app-user chown call and mode 700, and links `~/.codex/auth.json` into it.
28. `~/.codex`'s other contents (`skills/`, `sessions/`, sqlite files) untouched.
29. **`ENTRYPOINT_ENGINE=codex` -> the function returns immediately.** Asserted against the real codex-brain shape (`~/.codex` already a symlink to `${DATA_DIR}/codex`) with an OAuth `auth.json` in place: the credential survives byte-identical and no symlink loop is created. This is the reproduced catastrophic case; the test models the link, not an empty dir.
30. **`BOBI_AUTH=api_key` -> the function returns immediately**, and `materialize_codex_api_key_auth` still writes to the ephemeral `$HOME`, never the volume.
31. Second boot is a no-op; an existing volume `auth.json` is not disturbed.
32. Self-heal: a regular-file `~/.codex/auth.json` *newer* than a stale volume copy wins (`mv -f`) and is re-linked.
33. An existing symlink pointing elsewhere is replaced without aborting under `set -euo pipefail`.
34. The sweep resolves the link and renames the **volume** file to `.bak`, leaving an OAuth-shaped one alone; the OAuth fixture is captured from a real `codex login`.
35. `docker-entrypoint.sh` sources the script and calls the function, before the api-key materialization block.
36. `bash -n` and shellcheck on both files.

**Docker lane** (`-m docker`, not on every PR)

37. Real image, subscription, Claude brain: `/data/codex` exists app-owned, `~/.codex/auth.json` links into it, survives a restart.
38. Real image, subscription, **codex brain**: brain credential intact after boot.

**Live, operator-verified - the proof of work on the implementation PR**

39. In a real worker container: an **agent following the guide** (not a hand-typed command) hits a codex 401, runs `tool-login`, and the operator authorizes from the Slack post. Screenshot the post and `--status`; show the same still-running worker completing a real `codex exec` adversarial pass.
40. Restart the machine and show `codex exec` still authenticated. This is the step that fails today.

Tests 1-36 run on every PR; 37-40 do not, and the PR will say so rather than implying the coverage is automatic.

## Phases

1. **Spec approved.** Gate 1 from Zach. Nothing below starts before it.
2. **Spec threading + probe.** `resolve_spec`, required spec params, `AuthProbe`, result object. Tests 1-14, 26.
3. **`tool-login`.** Command, ordering, exit contract, `file_lock(blocking=)`, cooldown, orphan reaping, provenance + `--status`, and the `--channel` removal on both commands. Tests 15-25, 7.
4. **Durable `auth.json`.** `docker/codex-auth.sh`, sweep fix, new non-docker harness. Tests 27-36.
5. **Delivery.** `bobi/tool_library/codex/guide.md`, `docs/TOOL_LIBRARY.md`, and the `moda-agents` companion PR.
6. **Live proof.** Tests 37-40 on a real container, attached to the PR.

Phases 2-4 are one bobi-agent PR; splitting them ships a command with nowhere durable to write. Phase 5's companion PR lands with it, and the issue closes only when both are merged.

## Overlap map

Nothing here re-implements another open issue's fix.

| Issue | Its fix | This plan |
|---|---|---|
| #863 gateway guard too broad | narrow the **brain** guard's predicate | **no adjacency.** An earlier draft narrowed the guard to `target == "brain"`; dropped, because on a gateway team that would let an agent authenticate around its own gateway. The predicate is untouched for every target |
| #868 stale bubble kills login | retry `ensure_bubble(force_remint_of=...)` | **depends on it, and fails safely without it:** the channel is exercised at step 4 with a real send, before any device code is minted, so a rejected JOIN exits 1 having burned nothing. Lower risk here anyway - this fleet's event server is the remote Worker (`https://bobi-events.modalabs.workers.dev`, health 200), not the bundled in-memory one |
| #901 presence-not-validity gate | validity check in the entrypoint's **brain** gate | **the same lesson, one layer deeper, on a different file.** #901 fixes a `-f` test on the brain credential at boot; D replaces a `Path.is_file()` outcome check on the codex credential at runtime with a real probe. No shared code, no boot gate added for codex |
| #861 `credentials_exist` ignores `CLAUDE_CONFIG_DIR` | resolve the cred path from the config-dir env | **avoided by design.** The `auth.json` symlink keeps `$HOME`-relative resolution correct for codex, so `credentials_path()` is untouched. #861's parenthetical about `CODEX_HOME` becomes unnecessary rather than contested. See Q4 |
| #860 Slack channel paste destination | fix the paste-back instruction / accept top-level messages | **not applicable.** `device_poll` never waits for an inbound message. Untouched for the brain's `paste_back` |
| #522 / #479 codex API-key auth | materialize `auth.json` from `OPENAI_API_KEY` | disjoint, and now provably so: B is gated on subscription mode, so the api-key path's files, locations, and lifetimes are byte-identical (test 30) |

## Questionables

**Q1. Boot-time warm login for codex, or in-flight only?**
Recommendation: **in-flight only.** The credential is durable after B, so the ceremony is once per machine; the cost is that the first codex use on a fresh machine takes an operator round-trip. A boot path is a second code path for the same job and either delays boot on an absent operator or needs a detached background ceremony.

**Q2. Who gets @-mentioned, and where?**
The blocking design's one assumption is that a human sees the post inside 300 s, and a passive message in `#bobi-eng-team` may not clear that bar. Proposal: `BOBI_LOGIN_MENTION` (a user or group id), defaulting to unset; if unset, the post says so and the recommended timeout drops to something that does not pretend. **This is the question that decides whether blocking is the right shape at all** - if nobody can be pinged, fail-cheap is the better design and the plan should change.

**Q3. Is agent-triggered device login an acceptable trust boundary?**
A worker processing untrusted input can trigger a login post. The mitigations are a fixed destination, a heads-up before the code, provenance, a cancellation message, a cooldown, and `--status` as an unforgeable local check. What remains is the shape itself: a bot asking a human to enter a code is the exact pattern OpenAI's banner warns about, and no in-band control fixes that. The alternative is operator-initiated only, which forfeits in-flight recovery. Recommendation: ship agent-triggered with the mitigations, and revisit if the fleet grows a less trusted input path. **Zach's call.**

**Q4. #861 - close it, or leave it?**
This plan makes the `CODEX_HOME` half of #861 unnecessary. Its `CLAUDE_CONFIG_DIR` half is a real independent bug and should stay open on its own merits. Flagging because "prefer fewer issues" cuts the other way here.

**Q5. Does the brain target get a probe too?**
D gives codex a probe because presence is provably meaningless there. The claude brain has the identical weakness (#901 is a symptom). Changing the brain's outcome check is #901's call, so `probe` stays unset for claude here.

## Proof of work

- This spec: the Problem section is first-hand verification, and five of its findings were produced by running the real CLI or reproducing the failure rather than reasoning about it - the 401 signature, symlink write-through, `codex login status` returning 0 for a bogus key, no `auth.json` before authorization, and the codex-brain credential destruction that killed an earlier draft of change B. [Appendix A](#appendix-a--verification-transcript) is the raw transcript.
- Implementation PR: the test list above, with 1-36 running on the PR and 37-40 run by hand and attached. Test 39's Slack screenshot and test 40's post-restart `codex exec` are the two that prove the feature; the unit tests only prove the parts.

---

## Appendix A - verification transcript

Captured 2026-08-05 in a live eng-team worker container (Fly machine `d8d0926a026d28`, codex-cli 0.144.5, `BOBI_AUTH=subscription`, `brain: claude`).

**The 401, and the exit code**

```
$ codex exec -s read-only --skip-git-repo-check "reply OK" < /dev/null; echo $?
...
ERROR: unexpected status 401 Unauthorized: Missing bearer or basic authentication in header,
  url: https://api.openai.com/v1/responses, request id: req_a74fcecafe3c426889629cc0394c5870
1
```

`Missing bearer` means no header was sent at all, not that one was rejected. That string is the `unauth_re` signature in D.

**No credential, no key, no fallback**

```
$ ls ~/.codex/auth.json          -> No such file or directory
$ ls ~/.codex/config.toml        -> No such file or directory
$ echo "${CODEX_HOME:-<unset>}"  -> <unset>
$ echo "${OPENAI_API_KEY:+set}"  -> (empty: unset)
$ echo "${OPENROUTER_API_KEY:+set}" -> (empty: unset)
$ echo "${AICHAT_PLATFORM:-<unset>}" -> <unset>
```

**No durable home**

```
$ ls -d /data/codex        -> No such file or directory
$ readlink /home/bobi/.codex -> (empty: a real directory, not a symlink)
$ mount | grep -E 'on (/data|/) '
none      7.8G  /        <- container overlay, where ~/.codex lives (ephemeral)
/dev/vdc   15G  /data    <- volume, where /data/claude lives (durable)
```

**`codex login --device-auth` works, and the existing regexes match it**

Driven under a pty exactly as `auth_bootstrap._spawn_login` does, with `CODEX_HOME` pointed at a scratch dir and `OPENAI_API_KEY` popped. Terminated before authorizing; the code below is expired.

```
Welcome to Codex [v0.144.5]
OpenAI's command-line coding agent

Follow these steps to sign in with ChatGPT using device code authorization:

1. Open this link in your browser and sign in to your account
   https://auth.openai.com/codex/device

2. Enter this one-time code (expires in 15 minutes)
   9S1A-79NNG

Continue only if you started this login in Codex. If a website or another person gave you this code, cancel.
```

```
url_re  (https://auth\.openai\.com/codex/device\S*)  -> https://auth.openai.com/codex/device
code_re (\b([A-Z0-9]{4}-[A-Z0-9]{5})\b)              -> 9S1A-79NNG
```

After termination `$CODEX_HOME` contained only `log/` and **no `auth.json`** - an unauthorized device login writes no credential file (verification 14).

**codex writes through an `auth.json` symlink** (the basis for change B)

```
$ ln -s /tmp/cx-sym/vol/auth.json /tmp/cx-sym/home/auth.json
$ echo "sk-test-dummy-key-not-real" | CODEX_HOME=/tmp/cx-sym/home codex login --with-api-key
Successfully logged in

$ ls -la /tmp/cx-sym/home/auth.json
lrwxrwxrwx auth.json -> /tmp/cx-sym/vol/auth.json      <- symlink intact
$ ls -la /tmp/cx-sym/vol/auth.json
-rw------- 77 auth.json                                 <- content landed on the target, mode 600
$ python3 -c "import json;print(sorted(json.load(open('/tmp/cx-sym/vol/auth.json'))))"
['OPENAI_API_KEY', 'auth_mode']
```

**Running that block on a codex-BRAINED machine destroys the credential** (why B is gated - verification 29)

Simulating the existing codex-brain shape, where `~/.codex` is already a symlink to `${DATA_DIR}/codex`, so `~/.codex/auth.json` and `${codex_vol}/auth.json` are the same path:

```
$ ln -s /tmp/brainsim/data/codex /tmp/brainsim/home/.codex
$ cat /tmp/brainsim/data/codex/auth.json
{"tokens":{"refresh_token":"REAL-OAUTH"},"auth_mode":"chatgpt"}

# an ungated version of the block:
self-heal branch FIRED
  mv -n exit=0        <- silent no-op: source and destination are the same file
  rm -f exit=0        <- the live OAuth credential is deleted
ln exit=0
$ cat /tmp/brainsim/home/.codex/auth.json
cat: Too many levels of symbolic links
VERDICT: CREDENTIAL DESTROYED
```

Unrepairable on later boots: the self-heal predicate needs a regular file, and the path is now a loop.

**`codex login status` cannot be used as an auth probe** (the basis for change D)

```
$ CODEX_HOME=/tmp/cx-sym/home codex login status; echo "exit=$?"
Logged in using an API key - sk-test-***-real
exit=0

$ time ( CODEX_HOME=/tmp/cx-sym/home codex login status >/dev/null 2>&1 )
real  0m0.017s
```

Exit 0, in 17 ms with no network call, for a key that is obviously invalid. It reports the *shape* of what is on disk, never whether it authenticates.

**Start-time-only, confirmed**

```
$ grep -rn "run_bootstrap\|login-bootstrap" --include=*.py --include=*.sh . | grep -v tests/
bobi/cli.py:596            @main.command("login-bootstrap")     <- the command
bobi/auth_bootstrap.py:470 def run_bootstrap(...)               <- the library
docker/docker-entrypoint.sh:569  as_app bobi agent ... login-bootstrap   <- the ONLY automated caller
```

**`login-bootstrap --channel` is worker-reachable today** (Problem §7, change F)

```
$ bobi agent eng-team login-bootstrap --help        # run as the worker uid, no privilege gate
Options:
  --channel TEXT   Private chat channel or gateway conversation ref to post
$ grep -n "login-bootstrap" bobi/cli.py
3559:    "events", "costs", "doctor", "login-bootstrap", ...   <- registered on the AGENT group
$ sed -n '412,414p' docker/docker-entrypoint.sh
as_app() { ... gosu "${APP_USER}" ...                          <- boot runs it as the worker uid too
```

**No codex preflight for this team**

```
$ grep -c "name: codex" package/agent.yaml          -> 0
$ grep "^- name:" package/agent.yaml                 -> gh, gstack, moda-skills
$ grep -c tool_library package/agent.yaml            -> 0
$ diff package/tools/codex.md bobi/tool_library/codex/guide.md  -> differs (hand-authored team file)
$ ls -la /usr/local/bin/codex   -> -rwxr-xr-x 1 1001 1001 298500144 Jul 16 02:19  (baked in the base image)
```

**`BOBI_BRAIN` is already pinned before any `bobi agent` command body runs** (the basis for A's required-spec rule)

```
$ sed -n '168,175p' bobi/cli.py
def _bind_agent_runtime(name: str) -> Path:
    ...
    _pin_team_brain(root)        # -> set_process_brain_from_config(cfg)
```

**The entrypoint test file an earlier draft cited does not exist**

```
$ ls tests/test_gitops_c22.py                                      -> No such file or directory
$ grep -rln test_entrypoint_materializes_codex_api_key_auth_file tests/  -> (nothing)
$ grep -rln docker-entrypoint tests/                               -> tests/integration/test_container_image.py
```

## Notes

**Two adversarial rounds, both folded into the design rather than appended.**

*Round 1* (three lenses: correctness, scope, security/operations) found that the first draft's `--force` path would report success for a revoked credential, that its blocking lock would park seven workers inside a call the harness would kill, that its whole-directory `cp -an` / `rm -rf` migration was unsafe and re-ran every boot, that narrowing the gateway guard would let a gateway team's agent authenticate around its own gateway, that a device code was minted before the channel was ever exercised, and that the `moda-agents` companion change is the only channel that reaches this fleet's agents at all. One "verified" citation (`tests/test_gitops_c22.py`) was false.

*Round 2*, re-reviewing the revision, found that the revision had introduced worse: the narrowed single-file symlink block, run "for every engine", **destroyed the OAuth credential and created a symlink loop on every codex-brained machine** (reproduced above), still leaked a plaintext `OPENAI_API_KEY` onto the volume in api_key mode, and made the subscription sweep rename the link instead of the credential. It also found that `--force`'s sibling command `login-bootstrap --channel` is worker-reachable today, that the probe needed a timeout and a 401 signature rather than a bare exit code, that `fsutil.file_lock` cannot be taken non-blocking without a scoped change, that the "explicit Bash timeout" in the recipe was a comment that sets nothing, and that a provenance string printed in Slack is copyable and so is not a control. All of those are resolved above: B is gated to subscription-mode non-codex-brained teams, the sweep resolves the link, the probe is an `AuthProbe` with three outcomes, `file_lock` gains a parameter, the recipe uses a real `timeout`, and `--status` replaces the provenance string as the operator's actual check.

**Cross-model review is still owed on this spec.** Both rounds were **same-model** (Opus 5) because `codex` 401s in this container, which is the bug this plan specifies. This is the standing gap made concrete: the plan that fixes the missing second opinion could not get a second opinion. Run `codex exec` against this file once the implementation lands, and record the result on the tracking issue.
