# Subscription auth for CLI tools, in-flight: a worker that 401s on `codex` can be authorized over Slack without a restart

> **Status:** Draft - awaiting Gate 1 approval from Zach. No implementation until approved.
> **Tracking issue:** moda-labs/bobi-agent#958 · **Created:** 2026-08-05
>
> Every claim in **Problem** was verified first-hand inside a live eng-team worker container on 2026-08-05.
> Raw evidence is in [Appendix A](#appendix-a-verification-transcript).
> The design is post-four-rounds-of-adversarial-review; see [Notes](#notes) for what each round changed and what is still owed.
>
> **Two questions are waiting on Zach and are not decided here:** whether to ship the [trimmed v1](#q6-ship-the-trimmed-v1), and whether to [land F standalone](#q7-land-f-standalone-ahead-of-this-design) ahead of this design.

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

**It works.** Driving `codex login --device-auth` under a pty in this container printed the URL and one-time code, and both regexes matched verbatim against codex-cli 0.144.5 ([Appendix A](#appendix-a-verification-transcript)).

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
- **Presence of a credential file proves nothing, and neither does `codex login status`.** `codex login status` is local-only (17 ms, no network) and returns **exit 0** for a syntactically-valid but entirely bogus API key ([Appendix A](#appendix-a-verification-transcript)). Any design that decides "is codex authenticated?" from the filesystem or from `login status` will report success for a credential that 401s. This constraint drives [D](#d-the-outcome-is-decided-by-a-real-probe-never-by-a-file).

## Solution

Six changes. Each is small; the ordering is the order they matter.

### A. Let the ceremony target a CLI tool instead of the brain

`run_bootstrap(..., target="codex")` resolves `_SPECS["codex"]` directly.
Everything downstream (pty spawn, scrape, chat post, credential check) takes the **resolved spec as a required argument** rather than re-deriving it from process state.

Required, not defaulted, because the ambient default is actively wrong here: `bobi agent <name> ...` pins `BOBI_BRAIN` to the team's brain before any command body runs (`cli.py:174` `_bind_agent_runtime` -> `_pin_team_brain` -> `set_process_brain_from_config`).
So inside a codex `auth` run, `_active_spec()` returns the **claude** spec.
A missed threading site would not crash, it would silently check `~/.claude/.credentials.json`, find the brain's credentials, and report codex as authenticated.
`cli.py:613` is exactly that shape today, so the new command must not be modelled on it.

#### The signature is the backstop, not the mechanism - A needs a call-site sweep

An earlier draft of this section claimed the required parameter was self-enforcing: *"a missed site becomes a `TypeError` under test, not a silently wrong credential check."*
**That is false, and round 4 found the counter-example in the tree.**

`bobi/setup/harness.py:83` calls `auth_bootstrap.credentials_exist()` with **zero arguments**, and `tests/test_setup_harness.py:79` does `monkeypatch.setattr(auth_bootstrap, "credentials_exist", lambda home=None: False)`.
The real function is replaced before the call, so the required parameter never reaches it: **`bobi setup` raises `TypeError` in production while the suite stays green.**
A monkeypatched dependency defeats a signature-based guarantee entirely - the type checker never runs, and the stub absorbs whatever arity the caller uses.

So the rule changes shape. The sweep below is the mechanism; the required parameter is the backstop for anything the sweep missed:

| # | Site | Today | Under A |
|---|---|---|---|
| 1 | `bobi/cli.py:614` | `credentials_exist()` - no args | thread the resolved spec; already in scope |
| 2 | `bobi/auth_bootstrap.py:509` | `credentials_exist(home)` - pre-check | deleted on the tool path (D) |
| 3 | `bobi/auth_bootstrap.py:585` | `credentials_exist(home)` - outcome check | replaced by the probe (D) |
| 4 | `bobi/auth_bootstrap.py:511` | `credentials_path(home)` - log line | thread the spec |
| 5 | **`bobi/setup/harness.py:83`** | **`credentials_exist()` - no args** | **the missed site. Thread `_SPECS["claude"]` explicitly: this is the setup wizard on a dev Mac, and it means the claude credential regardless of any team's brain** |
| 6 | `bobi/auth_bootstrap.py:118` `needs_bootstrap` | `credentials_exist(home)` | **no production caller** (grepped: `bobi/` has none, only `tests/test_auth_bootstrap.py`). Thread it or delete it - do not leave a third spec-deriving entry point alive with no caller |
| 7 | `bobi/auth_bootstrap.py:107` `credentials_path` | `_active_spec()` | re-derivation; takes the spec |
| 8 | `bobi/auth_bootstrap.py:127` `_spawn_login` | `_active_spec()` | re-derivation; takes the spec |
| 9 | **`bobi/auth_bootstrap.py:182` `_read_until_url`** | **`_scrape_login(fd, timeout, _active_spec())`** | **live, not dead: called at `auth_bootstrap.py:536` on the claude `paste_back` path. It re-derives from process state after the caller already resolved the spec, which contradicts A's stated invariant. Takes the spec** |
| 10 | `bobi/auth_bootstrap.py:506` `run_bootstrap` | `_active_spec()` | becomes `resolve_spec(target, cfg)` |

**Two test stubs must be pinned to the real signature in the same PR**, or the next signature change repeats this exactly:

- `tests/test_setup_harness.py:79` - `credentials_exist`
- `tests/test_auth_bootstrap.py:362,404` - `_read_until_url`

Pin them with `unittest.mock.patch.object(..., autospec=True)` (or `create_autospec`), which validates the stub's signature against the real function, rather than a bare `monkeypatch.setattr` with a hand-written lambda. That is the only construct here that makes a signature change fail a test instead of passing one.

### B. Give `auth.json` a durable home - on exactly the teams that need it

Symlink **the single credential file**, not the directory:

```
~/.codex/auth.json  ->  ${DATA_DIR}/codex/auth.json
```

**Verified: codex writes through the symlink.** A real `codex login` against a symlinked `auth.json` left the symlink intact and landed `-rw-------` content on the target ([Appendix A](#appendix-a-verification-transcript)).

`~/.codex` stays on the image, holding what it holds today: `state_*.sqlite`, `logs_*.sqlite`, `sessions/`, `shell_snapshots/`, `skills/` (2.2 MB of live codex state).
That directory is image-owned and the team build mutates it by name (`agent.yaml` `build.run`, and the gstack `requires:` check reads it), so it must keep tracking the image.

**The block runs only when `BOBI_AUTH=subscription`, and only when `~/.codex/auth.json` is not already the volume file.** Both guards are load-bearing, and each was proven necessary by reproducing the failure it prevents ([Appendix A](#appendix-a-verification-transcript), [Appendix B](#appendix-b-round-4-reproductions)).

- **Not already the volume - guarded on the invariant, not on the engine.** On a codex brain the entrypoint already links `~/.codex -> ${DATA_DIR}/codex` (lines 89-91, 526-530), so `~/.codex/auth.json` and `${codex_vol}/auth.json` are *the same path*. An earlier draft ran this block for every engine, and the self-heal branch then did `mv -n a a` (silent no-op), `rm -f a` (**destroys the live OAuth credential**), `ln -sfnT a a` (**ELOOP**). Reproduced end to end.

  Round 2 fixed that with an **engine** guard (`[ "$4" != "codex" ] || return 0`). Round 4 found the engine is a *proxy* for the invariant, and the proxy fails: **flip a team codex -> claude and restart.** `~/.codex` is a symlink on the container overlay, and the overlay survives a same-machine restart - the entrypoint asserts exactly this in its own comment (`docker-entrypoint.sh:397-399`, *"a fresh image rootfs each deploy ships the real dir; a same-machine restart already has the link"*), and its codex block re-links only when the `readlink` differs, so on a restart it no-ops and the link stands. The engine is now `claude`, so the guard lets the block through onto a `~/.codex` that is still the volume. `[ -f ] && [ ! -L ]` is **true** (the file is a regular file reached through a directory symlink), so the self-heal fires: `mv -f X X` -> `mv: … are the same file` -> **exit 1** -> `set -euo pipefail` (line 14) **aborts the boot**, every boot, until someone intervenes.

  Round 2's `mv -n` -> `mv -f` change is what made this a boot-failure loop rather than a silent no-op, so the guard is load-bearing for **availability**, not just correctness. Reproduced in [Appendix B](#appendix-b-round-4-reproductions): the boot script exits 1 and never reaches its last line.

  So the guard is the invariant itself - *do the two paths already resolve to the same file?* - which is true on a codex brain, true after a codex -> claude flip, and false on exactly the machines that need the link. The `engine` parameter is **dropped**: its only job was to be a wrong proxy for this test, and keeping it would preserve the belief that made the bug.
- **Subscription only, and it must *remove* a surviving link, not merely return.** In `api_key` mode `materialize_codex_api_key_auth "${HOME}/.codex"` writes through any symlink, which puts a plaintext `OPENAI_API_KEY` on a snapshotted volume.

  **Correction, round 4: the earlier claim that this gating keeps #522's behaviour "byte-identical" was false**, and verification 30 passed only because it started from a clean `$HOME`. Boot in subscription mode (the link is created), set `BOBI_AUTH=api_key`, restart: the link is on the overlay and survives, the guard returns early **without removing it**, and `materialize_codex_api_key_auth` writes straight through it. Reproduced in [Appendix B](#appendix-b-round-4-reproductions): `{"OPENAI_API_KEY": "sk-proj-PLAINTEXT-SECRET"}` landed on the volume, mode 600, durable. That is verbatim the failure this bullet says the guard prevents.

  So the api_key branch **removes a surviving `auth.json` symlink** before returning. Only a symlink: on a codex brain in api_key mode `~/.codex/auth.json` is a regular file on the volume and #522 owns it, so `[ -L ]` is false there and nothing is touched. With the removal in place the claim holds for the population it is actually about - a non-codex-brained team in api_key mode gets the key on the ephemeral overlay, exactly as #522 does today - and the honest scope of the claim is *"api_key behaviour is restored to #522's, including after a mode flip"*, not *"byte-identical under all histories"*.

So the block does its work on exactly one population - subscription-mode teams whose `~/.codex/auth.json` is not already the volume file - which is precisely the set that has the bug.

Three details:

- **Ownership.** The entrypoint runs as root until `exec gosu` (line 630), and the recursive `chown` of `$DATA_DIR` is gated on the `.bobi-owned` stamp that every already-deployed machine already has. `mkdir -p "${DATA_DIR}/codex"` alone lands `root:root`, and the app user can then write neither the credential nor the lock file. The block chowns explicitly, mirroring lines 485-486.
- **Self-healing, local-wins.** If anything ever replaces the symlink with a regular file, that file is by definition the *newer* credential, so it wins: `mv -f` onto the volume, then re-link. An earlier draft used `mv -n`, which silently kept the stale volume copy and deleted the fresh one.
- **Nothing in bobi may write `auth.json` through the atomic helper.** This repo's `CLAUDE.md` states the hazard exactly: `atomic_write_text` "lands as a new inode renamed over the target, so the target's mode, ownership, and **symlink-ness do not survive**", which is why `config.save_bubble_state` stays off the helper on purpose. A single `atomic_write_json` against `~/.codex/auth.json` would silently convert the link back into a regular file and strand the credential on the ephemeral overlay. So the constraint is written down here, a test asserts it, and the self-heal above is the backstop rather than the plan. The precedent also covers the mode: the credential is created at `0600`, not chmod-ed after.

`CODEX_HOME` stays unset, so codex and `auth_bootstrap.credentials_path()` both resolve through `$HOME` and agree, and `credentials_path()` needs no change (which is why this plan does not touch #861's lines).
A dangling `~/.codex/auth.json` (volume file not yet created) behaves exactly like a missing file for both `Path.is_file()` and codex, so the pre-login state is correct by construction.

### C. Make the trigger in-flight, and bound it honestly

New command `bobi agent <name> auth codex`, callable by any worker in the container.

`--timeout` is the budget for the **whole command**, not just the device poll: the probe, the scrape (`url_timeout`, 120 s today), the channel work, and the poll all draw from it.
Default **300 s**, comfortably under the harness's 600 s Bash ceiling, and the recovery recipe tells the agent to raise the Bash tool's own `timeout` parameter, whose default is 120 s and would otherwise kill the ceremony mid-flight.
An earlier draft defaulted to 600 s, stacked the 120 s scrape on top, and expressed the Bash timeout as a shell comment - which sets nothing and would have made the graceful-timeout path unreachable.

There is deliberately **no `timeout 420` shell wrapper** around the call. An earlier draft of this paragraph said there was one, contradicting the recipe below, which had already dropped it: making `--timeout` the whole-command budget is what removes the need for a second timeout, and keeping both would only say the design does not trust itself. The recipe is the single source of truth for how the call is made.

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
This removes the presence pre-check entirely rather than making it skippable: the probe is the only gate, and it does not care what is on disk. A `--force` flag that skips a check nobody performs would be a trap, so there is none. `--rebind` (below) is the flag that covers the case people reach for `--force` for.

`"reply OK"` is fixed argv, never caller-controlled, so no review content reaches the probe.

### E. Single-flight that never blocks the herd

Eight workers share a container (`max_concurrent_agents: 8`) and can all 401 within seconds.

`bobi/fsutil.py::file_lock` gains `blocking: bool = True`; the new call site passes `blocking=False` (`LOCK_EX | LOCK_NB`).
Existing callers are unchanged by the default, so this stays one lock mechanism rather than a hand-rolled `fcntl` beside it.
It has to be a real parameter: `file_lock` today is an unconditional blocking `LOCK_EX` with no deadline, so queueing would park seven workers for the winner's full timeout inside a call each was told is bounded.
A loser returns **exit 3** immediately and degrades, which is the correct answer - its review is not the one that will benefit.

The same exit 3 carries the cooldown: a stamp beside the lock suppresses starting *another* ceremony within one timeout of the last, so a wedged agent looping on 401s neither spams the login channel nor burns device codes.
The cooldown is evaluated **after** the probe, so a healthy container always gets exit 0 rather than a spurious exit 3.

The control that stops an operator being pointed at a dead code is **editing the message to its expired state** (below), not the interval: the code stops existing in the channel the moment it stops being pollable.

### F. Close the arbitrary-destination hole on both commands

`auth` has no `--channel`; the destination is always `$BOBI_LOGIN_CHANNEL`.
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

1. `bobi/auth_bootstrap.py`: required-spec threading **across the full call-site sweep in [A](#the-signature-is-the-backstop-not-the-mechanism---a-needs-a-call-site-sweep)** (10 sites, including `_read_until_url` and `needs_bootstrap`), `run_bootstrap(target=...)`, `AuthProbe`, result object, send-path pre-flight, message-ref return + edit dispatch, orphan reaping, tool-target messaging.
2. `bobi/cli.py`: `auth` command (incl. `--status`, `--rebind`, and the specified `--help` text), with `login-bootstrap` kept as a hidden alias; `--channel` dropped from the agent-group registration.
2a. **`bobi/slack.py`: `update_slack_message` (`chat.update`).** Round 4 addition, and forced: without it the one-edited-message design does not run on the legacy branch, which is [the branch this fleet takes](#the-edit-has-to-work-on-the-legacy-slack-branch-which-is-the-one-this-fleet-takes).
2b. **`bobi/setup/harness.py`: the missed `credentials_exist()` call site**, threaded explicitly. Round 4 addition; it is not optional, because A's signature change makes `bobi setup` raise `TypeError` without it.
3. `bobi/doctor.py`: a codex auth row reading the same state file as `--status`.
4. `bobi/fsutil.py`: `file_lock(..., blocking=True)`. Default preserves every existing caller.
5. Non-blocking single-flight + cooldown, both surfacing as exit 3.
6. `docker/codex-auth.sh` (new, sourceable) + its call from `docker/docker-entrypoint.sh`: durable `auth.json` symlink on subscription teams whose `~/.codex/auth.json` is not already the volume file, app-user owned, self-healing, with the api_key branch removing a link left by a mode flip; subscription sweep made non-destructive and target-resolving.
7. `bobi/tool_library/codex/guide.md` + `docs/TOOL_LIBRARY.md`: the recovery contract.
8. **A companion PR in `moda-agents`** carrying the same recovery contract into `moda-eng-team`'s `tools/codex.md` and deleting its false preflight claim. Not optional; see [Delivery](#delivery-the-bobi-agent-pr-alone-does-not-close-958).
9. Tests per [Verification](#verification), including a fake-codex stub so the ceremony is testable without an operator.

### Out of scope

- **API-key auth for codex.** #522/#479 own it, and B is gated so their behaviour is preserved - including across a subscription -> api_key flip, which round 4 found the gate did *not* handle (see [B](#b-give-authjson-a-durable-home---on-exactly-the-teams-that-need-it)).
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
| `credentials_exist()` pre-check | returns True | **deleted** on the tool path; the probe is the only gate (D) |

**Gateway, unchanged.** `validate_auth_mode` already fatals on gateway + subscription (entrypoint:155-156), so gateway teams are always `api_key`, where the shadow-env guard refuses anyway. No team shape loses a capability it could otherwise have had, and leaving the predicate alone removes this plan's only adjacency with #863. An earlier draft narrowed the guard to `target == "brain"`; that is dropped, because on a gateway team it would let an agent run `codex login --device-auth` straight against `auth.openai.com` and route model traffic around the audit and spend boundary, with an operator's authorization on it.

**Shadow env, mode-aware,** because the current message cannot be followed. `OPENAI_API_KEY` is legal on a Claude-brained subscription team (`validate_auth_mode` only forbids the *brain's* shadow key, entrypoint:146) and `bobi/env.py` propagates it to every child, but subscription mode refuses to materialize codex api-key auth and sweeps any api-key file. So "use the #522 API-key path" points at a path this mode has already disabled:

- **`BOBI_AUTH=api_key`**: refuse, and name #522's path, which is genuinely available there.
- **`BOBI_AUTH=subscription`**: proceed, and **log a warning** naming the key. `_spawn_login` already does `env.pop(spec.shadow_env)` so the ambient key cannot contaminate the ceremony, and per #479 codex does not read `OPENAI_API_KEY` for `/v1/responses`, so it cannot outrank the OAuth credential at runtime either (verified: with an ambient key and no `auth.json`, codex sends **no bearer at all**). Refusing here would make codex permanently unauthenticatable on such a team with no working instruction.

The real shadowing vector is the *file*, not the env var, which is what the sweep below is for.

### The command: `bobi agent <name> auth [<tool>]`

The CLI mirrors the library layer A already builds. `resolve_spec(target)` treats the brain as one target among several, so the command should too: one verb, an optional target, `brain` implied when omitted.

```
bobi agent <name> auth [<tool>] [--timeout SECONDS] [--status] [--rebind]
```

`login-bootstrap` stays as a **hidden alias**, not a rename. `docker/docker-entrypoint.sh:566` in every already-published image invokes it by name, so breaking it would break rollback to any earlier image. The alias costs one line.

An earlier draft called this `tool-login`. Dropped: it pairs badly with its own sibling (`login-bootstrap` / `tool-login` share a word in opposite orders and differ on two axes at once), and it encodes an internal taxonomy the caller does not have - an agent hitting a 401 knows "codex is not authenticated", not "codex is a tool target".

| Field | Meaning |
|---|---|
| `<tool>` | a key in `_SPECS`; omitted means the team's brain, which is today's `login-bootstrap` behaviour |
| `--timeout` | total budget for the whole command: probe, lock, scrape, post, poll. Default 300 |
| `--status` | print local auth state and exit. Never posts, never spawns, never blocks |
| `--rebind` | remove the stored credential, then run the ceremony. For a wrong-account binding or a rotation |

There is deliberately **no `--channel`** (F), and deliberately **no `--force`**.
`--force` existed to skip a presence pre-check, and D deleted the presence pre-check: the probe is the only gate, and it does not care what is on disk. A flag that skips a check nobody performs is a trap.

`--rebind` is the flag that replaces it, and it closes a real hole rather than a cosmetic one. Ordering step 2 is "probe passes -> exit 0 without a ceremony", so a credential that **works but is bound to the wrong account** could never be replaced through this command. Every credential surface owes an answer to "someone left the team"; this is it.

| Exit | Meaning | Caller does |
|---|---|---|
| 0 | probe passes: the tool authenticates | run the cross-model pass |
| 4 | ceremony ran, nobody authorized in time | degrade to same-model, record the owed cross-model opinion |
| 3 | another worker is already running it, **or** one ran within the cooldown. The distinguishing reason is printed on stderr, because the two call for the same action but read very differently in a log | retry the tool once (it may have just been fixed), else degrade as for 4 |
| 5 | **probe inconclusive**: 429, 5xx, upstream outage, sandbox init failure. A login would not have helped | degrade; do not retry, do not page |
| 1 | misconfigured or unusable: no login channel, unknown tool, gateway team, `OPENAI_API_KEY` in api_key mode, channel send failed | report the error; do not retry, and do not page an operator |

**Nothing semantically loaded may sit on exit 2**, because `bobi` is a click CLI and `click.UsageError.exit_code == 2` (verified against the installed click 8.4.2; `NoSuchOption` inherits it).
An earlier draft assigned 2 to *"ceremony ran, probe still shows unauthenticated"*. Delivery (below) puts the baked recipe and the installed `bobi` on independent release trains, so skew is not hypothetical: an older `bobi` with no `auth` subcommand - the exact rollback case the hidden `login-bootstrap` alias exists to protect - exits 2 as a usage error, and the recipe would then record `OWED="operator did not authorize within the login window"` **verbatim in a review artifact, for a command that never ran.** A false statement about a human is the worst possible thing to auto-record. So that meaning moves to **4**, and 2 is left unassigned and unreachable by design.

Exit **1** stays the catch-all, and that is deliberate rather than an oversight: `click.ClickException.exit_code == 1`, so a click-level failure lands on the code whose recorded meaning is *"codex login is unavailable on this machine (see the error above)"* - which is true of a version skew. The collision that matters is the one where the two meanings disagree, and 2 was the only one.

Splitting **5** out of 1 is the same argument the table already makes for exit 3's two reasons: inconclusive and misconfigured call for the same action but read completely differently in an artifact a human later reads. "The tool is erroring upstream" and "this machine is set up wrong" are not the same claim.

`run_bootstrap` today returns a bare `bool` and swallows `TimeoutExpired` into the same `credentials_exist()` result, so it cannot express this. It returns a small result object (`ok`, `reason`), and the CLI maps `reason` to the exit code.

For a command whose entire purpose is a human ceremony at an inconvenient hour, the docstring is the primary interface, so it is specified here rather than left to the implementer:

```
$ bobi agent eng-team auth codex --help

Ask the operator to authorize a CLI tool over chat, and wait.

    codex authenticates with an OAuth subscription, not an API key. When it
    401s inside a running container, this posts a device-login request to
    $BOBI_LOGIN_CHANNEL and blocks until a human authorizes it or the window
    closes. One login authorizes every worker in the container, including
    ones already running, and survives a restart.

    The outcome is decided by a real call to the tool, never by a file on
    disk: a credential can be present and still be dead.

    Exit 0 authorized · 4 nobody authorized in time · 3 another worker is
    already running it · 5 the tool is erroring upstream, so a login would
    not have helped · 1 unusable (no login channel, unknown tool, gateway
    team). Exit 2 is never used: it is click's usage-error code.

Usage:
    bobi agent eng-team auth codex             # ask, and wait up to 5m
    bobi agent eng-team auth codex --status    # what is true now; asks nobody
    bobi agent eng-team auth codex --rebind    # drop the credential, ask again
    bobi agent eng-team auth --status          # same, for the team's brain

Options:
  --timeout SECONDS  How long to hold the run open while the operator gets to
                     their laptop. Whole-command budget, default 300. At the
                     deadline the pending code is cancelled in chat, exit 4.
  --status           Print local auth state and exit. Never posts, never waits.
  --rebind           Remove the stored credential and run the ceremony. Use when
                     the wrong account was bound, or to rotate.
```

`--status` prints state, not a table cell:

```
$ bobi agent eng-team auth codex --status
codex        unauthenticated (401, probed 40s ago)
credential   /data/codex/auth.json  absent
ceremony     in flight since 00:31:12 UTC, run wf-issue-lifecycle-eng-team-958, pid 4417
cooldown     n/a
```

### `doctor` is the front door; `--status` is the detail view

`bobi/doctor.py` already ships `_check_claude_auth()`, which runs a minimal real query and reads a 401 out of stderr - the exact pattern D specifies, already in this repo, already applied to the brain. An operator at 2am runs `doctor`; they do not run a flag on a command they have never heard of.

So `run_doctor()` gains a codex row that reports the last probe result and its age from the same state file `--status` reads. No network in a health check: it reports what is known, and `--status` or a real ceremony refreshes it. One front door, one detail view.

This is also supporting evidence for Q5: the house already agrees that presence is not enough for the brain, because `_check_claude_auth` makes a real call rather than stat-ing `.credentials.json`.

### Ordering inside the ceremony

Order is a correctness property here, and the current code has it wrong for this use: `run_bootstrap` spawns the pty, scrapes, and only *then* touches the channel. If the post fails, a device code has already been minted and burned, and the URL and code existed only in a subprocess's stderr. Nobody is told a login is pending.

1. **take the lock, non-blocking** - cheapest possible shedding, burns nothing; on failure exit 3
2. **probe** - if it passes, exit 0 without a ceremony; if inconclusive, exit 5
3. **cooldown** - if a ceremony ran within the interval, exit 3
3a. **`--rebind` only:** remove the credential - **after** every early exit above, never before (see below)
4. **channel pre-flight**: register, then **post the message in its "starting" state** ("codex login starting on machine X, run Y - code follows"), keeping the message ref `_post_login_message` now returns (the `ts` from `chat.postMessage` on the legacy branch, from `channels_send` on the gateway one). This is what actually validates the destination. Registration alone does not: `_register_login_channel` provisions workspace-level credentials and grants and never touches the channel id, so `channel_not_found`, `not_in_channel`, and a missing scope surface only on send. On failure exit 1, having burned nothing. It also gives the operator a few seconds' warning before the code appears.
5. **reap** an orphaned poller from a previous killed run (pidfile beside the lock)
6. **spawn** the pty, scrape URL + code
7. **edit** that same message (`mode="update"`, `edit_ref=ts`) to carry the URL, code, deadline, and verify line
8. **poll** until the remaining budget is spent
9. **reconcile** the symlink (B), **probe again**, **edit** the message to its terminal state - authorized, expired, or failed - and exit 0 / 2

Steps 4, 7 and 9 are **one message**, not three posts. Editing in place is what removes a live-looking code from the channel the instant it stops being pollable, so the "dead code in scrollback" problem is deleted rather than mitigated with a third message. This requires an edit path on **both** Slack branches, not just the gateway one - see [the legacy-branch correction](#the-edit-has-to-work-on-the-legacy-slack-branch-which-is-the-one-this-fleet-takes), which is what the fleet's own `BOBI_LOGIN_CHANNEL` actually resolves to.

#### Step 3a: `--rebind` must not destroy the credential without running a ceremony

> **Conditional fold.** This is a fix to `--rebind`, so it applies only if `--rebind` survives [Q6](#q6-ship-the-trimmed-v1). If the trimmed v1 ships, `--rebind` is cut and this subsection goes with it - nothing else depends on it.

An earlier ordering put the removal at step 2 (*"`--rebind` removes the credential **before** this step"*), so that a passing probe could not short-circuit a deliberate rotation. Read together with step 3, that destroys the credential and then declines to replace it: **`auth codex --rebind` inside the cooldown window deletes `auth.json`, then exits 3 without ever running a ceremony.** Codex is now dead for all eight workers in the container until the cooldown lapses and someone runs the command again. Same shape for a lock loss at step 1.

The fix is ordering: **removal moves to step 3a, after every early exit.** `--rebind` still skips the *probe's* exit-0 short-circuit - that is the whole point of the flag, and it is what makes replacing a wrong-account binding possible - but it now skips only that one branch, not the lock and not the cooldown. A `--rebind` that cannot proceed leaves the working credential exactly where it was.

Two things follow, and both are part of this fold:

- **The credential is moved, not deleted.** Step 3a renames it to `${codex_vol}/auth.json.rebind.bak` (the same `.bak` convention the api-key sweep already uses), and step 9 removes the `.bak` only after the probe passes. If the operator never shows up, the terminal state restores it. Without this, `--rebind` + absent operator is strictly worse than not running the command: exit 4 with the working credential gone and no way back short of a full ceremony.
- **It stays worker-invocable, and that is a deliberate acceptance rather than an oversight.** §7 and F rest on treating a worker processing a fork diff or an issue body as untrusted, and `--rebind` is a destructive action on shared state on that same surface. With the restore above, the worst a hostile invocation achieves is one ceremony's worth of disruption plus a cooldown - the same blast radius as calling `auth codex` in a loop, which the cooldown already bounds. Flagged because F's threat model demands the question be asked of every new verb on that command, and this is the answer for this one.

Step 5 exists because `_spawn_login` uses `start_new_session=True` and the `finally` that terminates the child does not run if the caller is SIGKILLed; without reaping, an orphaned `codex login` keeps polling OpenAI's token endpoint, one per killed run. Termination also escalates `terminate` -> `wait` -> `kill`, which it does not today.

A scrape timeout at step 6 exits 1 and edits the message to its failed state, so the operator who was told a code is coming is told it is not, rather than being left with a promise that never resolves.

### Notification and provenance

The whole case for blocking rests on an operator responding inside 300 s, and today `_post_login_message` posts a plain, anonymous message.

**One message, edited in place - not three.** An earlier draft posted a heads-up, then the code, then a cancellation. So the ceremony posts once and edits that message through its states: pending -> authorized, expired, or failed.

That is not just tidier. It **deletes** the dead-code-in-scrollback problem rather than mitigating it with a third post: there is never a live-looking code sitting above a cancellation notice, because the code is gone from the message the moment it stops being pollable.

#### The edit has to work on the *legacy* Slack branch, which is the one this fleet takes

Round 3 justified the design with *"`channels_send` already supports `mode="update"` with an `edit_ref`, and returns the `ts` needed to do it; `_post_login_message` merely discards that `ts` today."*
**That is true of only one of the two branches, and not the one that runs here.**

`_post_login_message` (`bobi/auth_bootstrap.py:288-296`) forks on `channel.legacy_slack_channel`:

```python
if channel.legacy_slack_channel:
    token = cfg.credential("slack", "bot_token")
    post_message(token, channel.destination, text)     # chat.postMessage
    return
_register_login_channel(project_path, cfg, channel)
from bobi.events.gateway import channels_send
channels_send(project_path, channel.destination, text, mode="post")
```

The live value in this container is `BOBI_LOGIN_CHANNEL=#bobi-eng-team` (verified in both `run/.env` and a worker's environment).
`_parse_conversation("#bobi-eng-team")` splits on `:` into **one** part, and the function returns `None` for anything that is not 4 or 6 parts - so `_resolve_login_channel` falls to `resolve_channel_id` and builds a `LoginChannel(legacy_slack_channel=…)`.
**The legacy branch is the branch this fleet takes**, and `bobi/slack.py` contains **zero** occurrences of `chat.update` (grepped).

So on the one deployment this plan exists to fix, step 7's edit is unimplementable as designed: it raises, or it silently degrades to three posts and the dead-code-in-scrollback problem returns in full.
Verification 25a as written - *"asserted by capturing the calls"* - would have passed **green** against the gateway branch while the real path degraded. A test that only exercises the branch the fleet does not take is not coverage.

**The fix: give the legacy branch an edit, rather than giving this fleet three posts.**
It is ~15 lines, and it makes one notification design work on every deployment shape instead of two designs racing:

- **`bobi/slack.py` gains `update_slack_message(token, channel, ts, text)`** - `_slack_api("chat.update", token, {"channel":…, "ts":…, "text": format_slack_message(text)})`. It mirrors `post_slack_message` (line 366) exactly, through the same `_slack_api` helper (line 181) that already raises on a non-`ok` response. `chat.update` takes the same `channel` + `ts` a `chat.postMessage` response returns, and a bot may edit its own message with no extra scope beyond the `chat:write` it already holds to post.
- **`_post_login_message` returns a message ref instead of `None`.** The legacy branch needs no new plumbing to produce one: `post_slack_message` **already returns the parsed API response** (`-> dict`, line 366), which carries `ts`; `_post_login_message` discards it today. The gateway branch takes `ts` from `channels_send`'s response, which the docstring already documents as *"the posted/updated message id"*.
- **A single `_edit_login_message(...)` dispatches on the same `channel.legacy_slack_channel` predicate** - `update_slack_message` for legacy, `channels_send(mode="update", edit_ref=ts)` for the gateway. One call site in the ceremony, one fork, in the same place the post already forks.

**If the edit fails, the ceremony does not.** An edit failure is logged and the run continues to its real exit code - the operator has a live code either way, and failing a successful authorization because a follow-up edit 500'd would be worse than a stale message. The post at step 4 keeps its hard contract (a send failure there is exit 1 with nothing minted); only the *edits* are best-effort. If the terminal edit fails, the ceremony appends a short terminal message as a fallback, which is the three-post behaviour scoped down to the case where the one-post design could not complete.

**Scope delta, stated because it is a real one:** this adds `bobi/slack.py` to the in-scope files, a file no earlier round touched. That is forced rather than chosen - without it the headline design of round 3 does not run on this fleet at all.

The pending state carries an **@-mention** (`$BOBI_LOGIN_MENTION`, see Q2), the agent, machine, run, a deadline, a runnable verification, and permission to ignore it:

```
*codex login needed* @<on-call>
`eng-team` on `d8d0926a026d28` (run `wf-issue-lifecycle-eng-team-958`) hit a codex 401.

Open this link, sign in, then enter the code:
https://auth.openai.com/codex/device
Code: `9S1A-79NNG`

Expires 00:20:14 UTC. Ignoring this costs nothing: the review runs single-model.
Not sure this is real? `fly ssh console -a moda-eng-team -s d8d0926a026d28 -C "bobi agent eng-team auth codex --status"`
```

The verify line is **runnable**, with the app and machine filled in from `$FLY_APP_NAME` and `$FLY_MACHINE_ID` (both present in a worker's environment, verified). An earlier draft told the operator to run `--status` without saying from where, which is a control with an unstated prerequisite: the operator is in Slack, probably on a phone, and cannot reach that machine's shell without the app id.

On success the message becomes the receipt an operator actually needs:

```
*codex login complete*
Authorized 00:18:02 UTC. `eng-team` on `d8d0926a026d28` resumed its review.
Bound account: <as reported by `codex login status`, or "not exposed by codex">
Wrong account? `… -C "bobi agent eng-team auth codex --rebind"`
```

On timeout it becomes `*codex login expired* - nobody authorized within 5m. The review ran single-model; the cross-model opinion is recorded as owed.`

**The security control is channel hygiene, and it belongs in the deployment contract.** `device_poll` binds whichever OpenAI account enters the code, and codex's own banner warns *"Continue only if you started this login in Codex. If a website or another person gave you this code, cancel."* A bot that routinely hands operators codes trains them past that warning, so an attacker who could post in the login channel could post a lookalike carrying a code from a login **they** started.

Provenance text does not defend against that - anything printed in Slack can be copied. Neither does `--status` alone, since it requires shell access. What actually defends against it is the one thing the earlier draft left unstated:

> **`$BOBI_LOGIN_CHANNEL` must be a channel only bobi can post to.** Any deployment where a human or a second app can post there has no defence against a lookalike login message, and should not enable agent-triggered login.

**Correction, round 4: this fleet does not satisfy that constraint today, and round 3 asserted it without checking.**
The live value is `BOBI_LOGIN_CHANNEL=#bobi-eng-team` - a human-facing team channel that people post in, resolved through `resolve_channel_id`'s `#name` path (verified in `run/.env` and in a worker's environment).
So the premise the constraint rests on is **false on the flagship deployment**, and the sentence *"Where that holds, a lookalike login message cannot be posted at all, and the question mostly closes"* does not apply here at all.

That inverts Q3 rather than narrowing it. Q3 is **not** "do we enforce this constraint or document it" - documenting a constraint the only deployment already violates changes nothing about the exposure. The real question is which of these to do before the first ceremony runs, and it is [Zach's call](#q3-is-agent-triggered-device-login-an-acceptable-trust-boundary):

1. **Move the login channel** to one only bobi can post to, and keep the constraint as written. Cheapest, and it makes the constraint true rather than aspirational - but it moves the message away from where the humans already are, which is the assumption the whole blocking design rests on (see Q2).
2. **Keep the human channel and accept the lookalike risk**, with the in-band mitigations (fixed destination, one edited message, cooldown, `--status`) doing the actual work rather than being belt and braces on top of a control that is not there.
3. **Enforce it** - `auth` refuses to run when it cannot verify the channel is bot-only. An extra API call and a new failure mode, and on today's config it would refuse to run at all, which means shipping this feature switched off.

The in-band mitigations do not close the gap on their own, so this is a real posture decision rather than a wording choice. What round 4 changes for certain is that the spec must stop presenting it as settled.

### Durable `auth.json`: `docker/codex-auth.sh`

Extracted into its own sourceable script so it can be tested without docker and without running the boot (`docker-entrypoint.sh` has no sourcing guard and interleaves top-level execution with its definitions). The entrypoint sources it and calls the function; a test asserts both the call and its position.

```sh
# docker/codex-auth.sh
# Durable codex OAuth for teams where codex is a TOOL, not the brain.
# Deliberately narrow: see plans/2026-08-05-codex-subscription-auth-in-flight.md §B.
codex_auth_link_volume() {           # $1 data_dir  $2 home  $3 owner  $4 auth_mode
  codex_vol="$1/codex"; codex_auth="$2/.codex/auth.json"

  # api_key: #522 owns codex auth and the key must stay on the ephemeral overlay.
  # Returning is NOT enough. A link left by an earlier subscription boot lives on
  # the overlay and survives a same-machine restart, and materialize_codex_api_key_auth
  # writes THROUGH it, landing a plaintext OPENAI_API_KEY on the volume. Only a
  # symlink is removed: on a codex brain this path is a regular file that #522 owns.
  if [ "$4" != "subscription" ]; then
    if [ -L "${codex_auth}" ]; then
      log "Removing stale codex auth.json symlink (auth mode is $4, not subscription)"
      rm -f "${codex_auth}"
    fi
    return 0
  fi

  # mkdir BEFORE the resolve: `readlink -f` needs every parent to exist, and on a
  # fresh machine both sides would otherwise resolve to the empty string and
  # compare EQUAL - skipping the link on exactly the boot that needs it.
  mkdir -p "${codex_vol}" "$2/.codex"

  # Guard on the INVARIANT, not the engine: if both paths already resolve to the
  # same file, ~/.codex IS the volume. True on a codex brain (the entrypoint links
  # the whole dir in §3b) and still true after a codex->claude flip, because that
  # link survives the restart. Doing the work anyway means `mv -f X X`, which exits
  # 1 and aborts the boot under `set -euo pipefail`.
  codex_auth_real="$(readlink -f "${codex_auth}" 2>/dev/null || true)"
  codex_vol_real="$(readlink -f "${codex_vol}/auth.json" 2>/dev/null || true)"
  if [ -n "${codex_auth_real}" ] && [ "${codex_auth_real}" = "${codex_vol_real}" ]; then
    return 0
  fi

  chown "$3:$3" "${codex_vol}"; chmod 700 "${codex_vol}"
  if [ -f "${codex_auth}" ] && [ ! -L "${codex_auth}" ]; then
    mv -f "${codex_auth}" "${codex_vol}/auth.json"    # local file is the NEWER credential
  fi
  ln -sfnT "${codex_vol}/auth.json" "${codex_auth}"   # -f: an existing path must not abort the boot
  chown -h "$3:$3" "${codex_auth}"
}
```

**The call site's position is now a correctness property, and is asserted (verification 35).**
The invariant guard reads `~/.codex`, so it must run **after** the entrypoint's codex-brain block (§3b, lines 484-533), which is what makes `~/.codex` the volume link on a codex brain, and **before** the api-key materialization block (line 536), which is the writer the api_key branch is protecting.
Dropping the `engine` parameter is what makes this a stated, tested precondition rather than a guard that quietly compensates for a call site nobody checked.

The subscription sweep is fixed in the same script.
Two bugs, both from the credential now living behind a link:

- It renames the **symlink**, not the credential, so the api-key file survives on the volume and is re-linked every boot - the sweep would neutralize nothing while appearing to work. It must resolve the target (`readlink -f`) and act there.
- `rm -f` on durable state is too sharp. `codex_auth_uses_api_key` keys on `data.get("OPENAI_API_KEY") and not data.get("tokens")`, and `tokens` is codex-internal and unversioned; a codex release that renames or nests it would make every boot read valid OAuth as an api-key file. Today that costs one container lifetime; behind the link it would force a device-login ceremony every boot forever, which Problem §4 already rejects as not shippable. So the sweep **renames the resolved target** to `${codex_vol}/auth.json.api-key.bak` and logs. The `.bak` is durable and observable, and the blast radius of a wrong predicate drops from destruction to a stale file. A dangling link then reads as "no credential", which is correct.

### The recovery contract agents follow

**Precondition, stated above the block rather than commented inside it:** set the Bash tool's `timeout` parameter to **480000** for this call. Its default is 120 s and would kill the ceremony mid-flight. An earlier draft expressed this as a shell comment, which sets nothing - the same class of mistake C already caught once.

```bash
review() { codex exec -s read-only -c 'model_reasoning_effort="high"' "$1" < /dev/null; }

review "$PROMPT" || {
  bobi agent "$BOBI_INSTANCE" auth codex
  case $? in
    0) review "$PROMPT" ;;                 # authorized: the pass runs
    3) review "$PROMPT" || OWED="another worker is running the login, or one just finished" ;;
    4) OWED="operator did not authorize within the login window" ;;
    5) OWED="codex is erroring upstream (429/5xx/outage); a login would not have helped" ;;
    *) OWED="codex login is unavailable on this machine (see the error above)" ;;
  esac
}
# If $OWED is set: run the pass single-model and record that line verbatim under
# "cross-model opinion owed:". Do not re-run `auth codex`.
```

`review()` is defined once so the two invocations cannot drift, and it carries the same `model_reasoning_effort="high"` the rest of the codex guide uses.
`$BOBI_INSTANCE` is the agent slot name (`eng-team` here), verified present in a worker's environment and valid as the `bobi agent` argument. There is no `$BOBI_AGENT_NAME`.

No `--force` (deleted), no `--timeout` (300 is the default), and no `timeout 420` wrapper: C already makes `--timeout` the whole-command budget, so wrapping it in a second timeout only says the design does not trust itself.

One Bash call for the login, one turn, no polling.

### Delivery: the bobi-agent PR alone does not close #958

`bobi/tool_library/codex/guide.md` reaches **zero** eng-team agents.
`package/agent.yaml` has no `tool_library:` key, so nothing from `bobi/tool_library/` is expanded for this team; and `docs/TOOL_LIBRARY.md:119` records that a team's own `tools/<name>.md` wins anyway, which eng-team ships.

So the agent-facing half is delivered exclusively by the `moda-agents` companion PR (In scope #8).
Without it the bobi-agent PR ships a working CLI command that nothing invokes, every agent keeps reading the false "preflighted (installed + authed)" claim, and #958's "Done means" is unmet.
The library guide is still worth updating for teams that do use `tool_library: [codex]`; it is just not the delivery channel here.

Verification 39 therefore drives the flow **through an agent following the guide**, not a hand-typed `auth` run - a hand-typed run would pass green while the agent-facing path stayed unreachable.

#### Tests 39 and 40 cannot gate this PR, and the plan should not pretend otherwise

An earlier draft called tests 39/40 *"the proof of work on the implementation PR"* and never mentioned the lag. **They are not obtainable at merge time.** The sequence is:

```
merge bobi-agent PR -> bobi release (PyPI) -> moda-agents pack release
  -> image rebuild (TEAM_DEPS bake) -> fleet roll -> THEN tests 39/40 can run
```

Every step after the first is post-merge, and the fleet roll is human-driven and outside this role.
The deployment surface *"consumes this repo as a RELEASED PyPI version (`pip install bobi==<pin>`), never a checkout"* (`CLAUDE.md`), so no amount of CI on this PR reaches a real worker container running this code.
And the two PRs cannot be mechanically linked: **`moda-labs/bobi-agent` is public and `moda-labs/moda-agents` is private** (verified), so a public PR's checks cannot depend on a private repo's PR. *"The issue closes only when both are merged"* is a discipline, not an enforcement mechanism, and is stated here as one.

So the evidence is split honestly, and the PR says which is which rather than implying the coverage is automatic:

| Stage | Evidence | When |
|---|---|---|
| **At merge** (what the PR can actually prove) | tests 1-36, including the new non-docker `tests/test_codex_auth_sh.py` harness and the fake-codex ceremony tests. Plus the docker lane (37/38) run manually, since it needs only a built image, not a released one | on the PR |
| **After the roll** (what closes #958) | tests 39/40 - an agent following the baked guide authorizing over Slack, and codex still authenticated after a machine restart | post-merge, post-release, post-roll |

**#958 stays open until 39/40 are run and their evidence is posted on the issue.** That is the point of the split: merging is not closing, and the ticket is the thing that carries the gap. Whoever performs the fleet roll runs them; the plan names it as an owed step instead of burying it in a proof-of-work bullet that reads as if it happened at merge.

This also means the **first** real exercise of the ceremony happens on a live fleet rather than in CI, which is an argument for keeping the surface small - see [Q6](#q6-ship-the-trimmed-v1).

## Relevant files

### Existing, verified 2026-08-05

- `bobi/auth_bootstrap.py` - `_SPECS` (codex spec 82-92, verified working), `credentials_path` 104, `credentials_exist` 110, `_spawn_login` 124 (`env.pop(shadow_env)` 132, `start_new_session=True` 138), `_scrape_login` 145, `_register_login_channel` 248 (workspace-level only, never touches the channel id), `_post_login_message` 288, `_ensure_discord_paste_back_ready` 299 (the validate-before-spawn precedent), `run_bootstrap` 470, `url_timeout=120` 474, gateway guard 494, shadow-env guard 515, spawn 531, scrape 558, post 563, `proc.wait` 573, terminate 577-583, `ok = credentials_exist(home)` 585.
- `bobi/cli.py` - `_bind_agent_runtime` 168-175 / `_pin_team_brain` 178-200 (pins `BOBI_BRAIN` on every `bobi agent` call); `login-bootstrap` 596-626 incl. the bare `credentials_exist()` at 614; agent-group registration 3557-3561; top-level pop list 3574. The `@click.option` at 597-599 is what makes the `--channel` flag exist; the registration is what makes the *command* reachable.
- **`bobi/slack.py`** - `_slack_api` 181 (the generic endpoint POST every call goes through, raises on non-`ok`), `resolve_channel_id` 311 (**explicitly accepts a `#name`, which is the fleet's live `BOBI_LOGIN_CHANNEL`**), `post_slack_message` 366 (**returns the parsed response dict, whose `ts` `_post_login_message` discards today**), `format_slack_message`. **Zero occurrences of `chat.update`** - grepped; this is blocker 1's root.
- **`bobi/setup/harness.py:81-83`** - `_oauth_credentials_present()` -> `auth_bootstrap.credentials_exist()` with **no arguments**. The call site change A missed.
- **`tests/test_setup_harness.py:79`** - `monkeypatch.setattr(auth_bootstrap, "credentials_exist", lambda home=None: False)`. Why the suite stays green while `bobi setup` would crash.
- `bobi/events/gateway.py:61-80` - `channels_send(..., mode="post|update|final", edit_ref=…)`, returns the response dict whose `ts` is *"the posted/updated message id"*. The gateway half of the edit, and the half round 3 correctly described.
- `docker/docker-entrypoint.sh` - codex-brain cred dir 89-91, `as_app` 412 (`gosu "${APP_USER}"`), skills + dir symlink 480-530 (incl. the `chown` at 485-486), `materialize_codex_api_key_auth` 317-332, `codex_auth_uses_api_key` 334-353, api-key materialization 536-549, subscription sweep 551-561, brain bootstrap 566-570, `exec gosu` 630, `.bobi-owned` chown gate 368-377. No sourcing guard; top-level execution is interleaved with definitions.
  Two lines are load-bearing for §B and were not cited before round 4: **`set -euo pipefail` at line 14** (which is what turns a failed `mv` into a boot abort), and the overlay-persistence assertion at **397-399** - *"a fresh image rootfs each deploy ships the real dir; a same-machine restart already has the link"* - which the codex block at 529-533 relies on identically, re-linking only when the `readlink` differs. `configure_brain_paths` sets `BRAIN_CRED_DIR="${DATA_DIR}/codex"` for **every** codex shape, gateway or not, so the invariant guard's equality test holds for all of them.
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

  Worth naming: `CLAUDE.md` records a standing blind spot - *"no canary exercises `auth: subscription`, the mode most fleet teams run, because a fresh subscription volume triggers a device login that blocks on a human"* (`plans/2026-08-01-ci-coverage.md` Q3). A device-login stub that plays back a real banner and completes on a trigger file is the missing half of that. Closing Q3 is not in this plan's scope, but the fixture should be built where that plan can reuse it rather than buried in a test module.
- `tests/fixtures/codex-device-login.txt`, `tests/fixtures/codex-auth-oauth.json` - the recorded banner, and a real `codex login` output captured for the sweep test rather than hand-written.
- `plans/2026-08-05-codex-subscription-auth-in-flight.md` - this file.

## Verification

No frontend, so no QA phase.

**Target resolution and guards** (`tests/test_auth_bootstrap.py`)

1. `run_bootstrap(target="codex")` on a Claude-brained team drives `codex login --device-auth` and checks `~/.codex/auth.json`, with `BOBI_BRAIN` still pinned to `claude` throughout.
2. Every spec-taking helper rejects a missing spec (`TypeError`).
2a. **The call-site sweep, asserted rather than trusted.** `bobi/setup/harness.py`'s `_oauth_credentials_present()` is called with the **real** `auth_bootstrap.credentials_exist` (not a stub) and does not raise - the regression test for the exact miss round 4 found, which today's suite cannot see because `tests/test_setup_harness.py:79` monkeypatches the function away.
2b. The two stubs that hide signature changes are pinned to the real signature with `autospec` (`credentials_exist` in `tests/test_setup_harness.py`, `_read_until_url` in `tests/test_auth_bootstrap.py`), so the next arity change fails a test instead of passing one.
2c. A source-level assertion that no module under `bobi/` calls a spec-taking helper without a spec - the sweep's own backstop, so site 11 does not appear later.
3. `target="brain"` is byte-for-byte today's behaviour.
4. The gateway guard fires for **both** targets on a gateway team.
5. `OPENAI_API_KEY` + `api_key` -> exit 1 naming #522. `OPENAI_API_KEY` + `subscription` -> proceeds, warning logged.
6. Unknown tool -> exit 1 listing known targets.
7. `auth` has no `--channel` and no `--force`; **`login-bootstrap` no longer has a `--channel` either** on the agent group; the `login-bootstrap` alias still resolves so a published image's entrypoint keeps working; the boot path still reads `$BOBI_LOGIN_CHANNEL`.
7a. **No exit code the command assigns is 2**, so a click `UsageError` from a version skew can never be read as an outcome. Asserted over the reason -> exit-code map, not per-case.

**Probe** (fake-codex stub)

8. Probe passes at entry -> exit 0, no pty, no post.
9. Ceremony runs, trigger appears -> exit **0**.
10. Ceremony runs, no trigger, probe emits the real 401 text -> exit **4**.
11. **The regression that motivates D:** a pre-existing `auth.json` whose probe 401s, operator absent -> exit **4**, not 0, with the file on disk throughout.
11b. `--rebind` removes a credential whose probe *passes* and runs the ceremony anyway, which is the only way to replace a wrong-account binding.
11c. **`--rebind` never destroys a credential it is not going to replace** (conditional on Q6 keeping the flag): inside the cooldown -> exit 3 **with `auth.json` still present and probing**; on a lost lock -> exit 3, same. And `--rebind` + absent operator -> exit 4 with the credential restored from `.rebind.bak`, not gone.
12. Probe exits non-zero with a 429/5xx body -> exit **5**, no ceremony, no post, no page.
13. Probe hangs -> bounded by its own timeout, classified inconclusive -> exit **5**.
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

**Operator-facing surfaces**

25a. **Parametrized over BOTH Slack branches, because the fleet takes the one round 3 did not test.** The ceremony posts **one** message and edits it, asserted by capturing the calls so a regression back to three posts fails:
  - **legacy** (`BOBI_LOGIN_CHANNEL="#name"`): one `chat.postMessage`, then `chat.update` carrying the same `ts` for the authorized / expired / failed states, and **zero** additional `chat.postMessage` calls.
  - **gateway** (`BOBI_LOGIN_CHANNEL="slack:T…:channel:C…"`): `channels_send(mode="post")` once, then `mode="update"` with the returned `edit_ref`.

  The single-branch version of this test is what round 4 found: it passed green while the real path degraded to three posts. Parametrizing it is the fix to the test, not just to the code.
25a-i. `_resolve_login_channel` **pins which branch each `BOBI_LOGIN_CHANNEL` shape takes** - `"#bobi-eng-team"` (the fleet's live value) resolves to `legacy_slack_channel`, a 4- or 6-part conversation ref does not. Without this a future reader repeats round 3's mistake of designing against the branch they happened to read.
25a-ii. An edit that fails (`chat.update` raises, or `channels_send(mode="update")` does) is logged and **does not change the exit code**; the terminal-state fallback posts a short follow-up message. A successful authorization is never reported as a failure because a follow-up edit 500'd.
25b. The pending message contains a runnable `fly ssh console -a $FLY_APP_NAME -s $FLY_MACHINE_ID -C "..."` line, and the success message names the bound account or says codex does not expose it.
25c. `--status` output includes the probe result and its age, the credential path and presence, in-flight run/pid, and cooldown remaining.
25d. `doctor` grows a codex row that reads the same state file and makes **no** network call.
25e. `auth.json` is never written through `atomic_write_text` / `atomic_write_json` anywhere in `bobi/` - a source-level assertion, because that helper renames a new inode over the target and would silently destroy the symlink (`CLAUDE.md`, durable-state rule).

**Scraper**

26. `_scrape_login` against the recorded 0.144.5 banner yields the URL and a well-formed code.

**`docker/codex-auth.sh`** (`tests/test_codex_auth_sh.py`, non-docker, runs on every PR)

27. Subscription + non-codex brain: creates `${DATA_DIR}/codex` with the app-user chown call and mode 700, and links `~/.codex/auth.json` into it.
28. `~/.codex`'s other contents (`skills/`, `sessions/`, sqlite files) untouched.
29. **Codex-brain shape -> the function returns without touching anything.** Asserted against the real shape (`~/.codex` already a symlink to `${DATA_DIR}/codex`) with an OAuth `auth.json` in place: the credential survives byte-identical and no symlink loop is created. This is the reproduced catastrophic case; the test models the link, not an empty dir.
29a. **The codex -> claude flip (blocker 4).** Same shape as 29, but the function is called with `auth_mode=subscription` and no engine argument at all - i.e. the machine whose team flipped to a claude brain and restarted, whose overlay link survived. **The whole call must run under `set -euo pipefail` and the script must reach its last line**: the pre-fix version exits 1 on `mv -f X X` and aborts the boot. Asserting only "the credential survived" is not enough - 29 already passes on the broken version, because the credential does survive; it is the *boot* that dies. So the assertion is on the exit status and on a sentinel echoed after the call.
29b. **Fresh machine, nothing exists yet.** `readlink -f` needs its parents to exist, so both sides would resolve to the empty string and compare equal, skipping the link on exactly the boot that needs it. Asserts the link IS created when `${DATA_DIR}/codex` and `~/.codex` are both absent at entry.
30. **`BOBI_AUTH=api_key` -> the function returns without linking, and the volume never receives the key. The test starts DIRTY.** It seeds a `~/.codex/auth.json` symlink left by an earlier subscription boot - exactly what survives a mode flip on the overlay - then runs the function, then runs `materialize_codex_api_key_auth`, then asserts **`${DATA_DIR}/codex/auth.json` does not exist** and the plaintext key landed on the ephemeral `$HOME` instead. The clean-`$HOME` version of this test is what let blocker 5 through: it passed while a plaintext `OPENAI_API_KEY` was reproducibly landing on the volume.
30a. Codex brain + `api_key`: `~/.codex/auth.json` is a regular file on the volume that #522 owns, so the api_key branch's `rm` does **not** fire. Guards the removal against over-reach.
31. Second boot is a no-op; an existing volume `auth.json` is not disturbed.
32. Self-heal: a regular-file `~/.codex/auth.json` *newer* than a stale volume copy wins (`mv -f`) and is re-linked.
33. An existing symlink pointing elsewhere is replaced without aborting under `set -euo pipefail`.
34. The sweep resolves the link and renames the **volume** file to `.bak`, leaving an OAuth-shaped one alone; the OAuth fixture is captured from a real `codex login`.
35. `docker-entrypoint.sh` sources the script and calls the function **after** the codex-brain link block (§3b, lines 484-533) and **before** the api-key materialization block (line 536). Both bounds are asserted: the guard reads `~/.codex`, so §3b is what makes the invariant true on a codex brain, and the materialization block is the writer the api_key branch protects. Position is a correctness property now that the engine parameter is gone.
36. `bash -n` and shellcheck on both files.

**Docker lane** (`-m docker`, not on every PR)

37. Real image, subscription, Claude brain: `/data/codex` exists app-owned, `~/.codex/auth.json` links into it, survives a restart.
38. Real image, subscription, **codex brain**: brain credential intact after boot.

**Live, operator-verified - the proof of work on the implementation PR**

39. In a real worker container: an **agent following the guide** (not a hand-typed command) hits a codex 401, runs `auth codex`, and the operator authorizes from the Slack post. Screenshot the single message through all three states (pending -> authorized) and `--status`; show the same still-running worker completing a real `codex exec` adversarial pass.
40. Restart the machine and show `codex exec` still authenticated. This is the step that fails today.

Tests 1-36 run on every PR; 37-40 do not, and the PR will say so rather than implying the coverage is automatic.

## Phases

1. **Spec approved.** Gate 1 from Zach, plus rulings on [Q6](#q6-ship-the-trimmed-v1) and [Q7](#q7-land-f-standalone-ahead-of-this-design). Nothing below starts before it.
2. **Spec threading + probe.** `resolve_spec`, required spec params **across all 10 sweep sites**, the two `autospec` stub pins, `AuthProbe`, result object. Tests 1-14, 2a-2c, 26.
3. **`auth`.** Command + hidden `login-bootstrap` alias, ordering, exit contract (no code on 2), `file_lock(blocking=)`, cooldown, orphan reaping, one edited message **on both Slack branches** (`bobi/slack.py`'s `chat.update` + the `_post_login_message` ref return), `--status`, `--rebind`, the doctor row, and the `--channel` removal on both commands. Tests 15-25, 25a-25a-ii, 7, 7a, 11b, 11c.
4. **Durable `auth.json`.** `docker/codex-auth.sh` with the invariant guard and the api_key link removal, sweep fix, new non-docker harness. Tests 27-36.
5. **Delivery.** `bobi/tool_library/codex/guide.md`, `docs/TOOL_LIBRARY.md`, and the `moda-agents` companion PR.
6. **Live proof - after the roll, not at merge.** Tests 39/40 on a real container, posted to **the issue**, which stays open until they are. See [the release-lag correction](#tests-39-and-40-cannot-gate-this-pr-and-the-plan-should-not-pretend-otherwise); the docker lane (37/38) does run at PR time, since it needs a built image rather than a released one.

Phases 2-4 are one bobi-agent PR; splitting them ships a command with nowhere durable to write. Phase 5's companion PR lands with it. The issue closes on phase 6's evidence, not on either merge - and nothing mechanical enforces that, because a public repo's PR cannot depend on a private one's.

## Overlap map

Nothing here re-implements another open issue's fix.

| Issue | Its fix | This plan |
|---|---|---|
| #863 gateway guard too broad | narrow the **brain** guard's predicate | **no adjacency.** An earlier draft narrowed the guard to `target == "brain"`; dropped, because on a gateway team that would let an agent authenticate around its own gateway. The predicate is untouched for every target |
| #868 stale bubble kills login | retry `ensure_bubble(force_remint_of=...)` | **depends on it, and fails safely without it:** the channel is exercised at step 4 with a real send, before any device code is minted, so a rejected JOIN exits 1 having burned nothing. Lower risk here anyway - this fleet's event server is the remote Worker (`https://bobi-events.modalabs.workers.dev`, health 200), not the bundled in-memory one |
| #901 presence-not-validity gate | validity check in the entrypoint's **brain** gate | **the same lesson, one layer deeper, on a different file.** #901 fixes a `-f` test on the brain credential at boot; D replaces a `Path.is_file()` outcome check on the codex credential at runtime with a real probe. No shared code, no boot gate added for codex |
| #861 `credentials_exist` ignores `CLAUDE_CONFIG_DIR` | resolve the cred path from the config-dir env | **avoided by design.** The `auth.json` symlink keeps `$HOME`-relative resolution correct for codex, so `credentials_path()` is untouched. #861's parenthetical about `CODEX_HOME` becomes unnecessary rather than contested. See Q4 |
| #860 Slack channel paste destination | fix the paste-back instruction / accept top-level messages | **not applicable.** `device_poll` never waits for an inbound message. Untouched for the brain's `paste_back` |
| #522 / #479 codex API-key auth | materialize `auth.json` from `OPENAI_API_KEY` | disjoint, and now provably so **including across a mode flip**: the api_key branch removes a link an earlier subscription boot left behind, so the key lands on the ephemeral overlay exactly as #522 does today. Round 4 found the earlier gate leaked a plaintext key onto the volume here, and that test 30 passed only because it started from a clean `$HOME`; 30 now starts dirty, and 30a guards the removal against over-reach |

## Questionables

### Q1. Boot-time warm login for codex, or in-flight only?
Recommendation: **in-flight only.** The credential is durable after B, so the ceremony is once per machine; the cost is that the first codex use on a fresh machine takes an operator round-trip. A boot path is a second code path for the same job and either delays boot on an absent operator or needs a detached background ceremony.

### Q2. Who gets @-mentioned, and where?
The blocking design's one assumption is that a human sees the post inside 300 s, and a passive message in `#bobi-eng-team` may not clear that bar. Proposal: `BOBI_LOGIN_MENTION` (a user or group id), defaulting to unset; if unset, the post says so and the recommended timeout drops to something that does not pretend. **This is the question that decides whether blocking is the right shape at all** - if nobody can be pinged, fail-cheap is the better design and the plan should change.

### Q3. Is agent-triggered device login an acceptable trust boundary?
**Reopened by round 4, not narrowed.** Round 3 answered this with a deployment constraint - `$BOBI_LOGIN_CHANNEL` must be a channel only bobi can post to - and concluded the question "mostly closes". It asserted that without checking the fleet against it.

**The fleet fails the constraint today.** `BOBI_LOGIN_CHANNEL=#bobi-eng-team` is a human team channel (verified in `run/.env` and in a worker's environment). So this is not "document vs enforce" - documenting a constraint the only deployment already violates changes nothing about the exposure, and the in-band mitigations round 3 demoted to "belt and braces" are, as things stand, the only controls there are.

The three real options are laid out [where the constraint is stated](#notification-and-provenance): move the channel, accept the risk with the in-band mitigations doing the actual work, or enforce it (which on today's config means shipping the feature switched off). Recommendation: **move the login channel to a bot-only channel**, because it makes the constraint true rather than aspirational and costs one config change - but it trades against Q2, since a channel humans do not sit in is a worse place to expect a response inside 300 s. **Zach's call, and it should be made before the first ceremony runs rather than after.**

### Q4. #861 - close it, or leave it?
This plan makes the `CODEX_HOME` half of #861 unnecessary. Its `CLAUDE_CONFIG_DIR` half is a real independent bug and should stay open on its own merits. Flagging because "prefer fewer issues" cuts the other way here.

### Q5. Does the brain target get a probe too?
D gives codex a probe because presence is provably meaningless there. The claude brain has the identical weakness (#901 is a symptom). Changing the brain's outcome check is #901's call, so `probe` stays unset for claude here.

---

### Q6. Ship the trimmed v1?

**Raised by the round-4 reviewer, escalated rather than decided. Not mine to call, and deliberately not folded either way.**

The proposal: cut **`--rebind`, `--status`, the doctor row, and the cooldown + orphan reaper** from v1. Keep **D** (the probe), keep E's non-blocking lock and immediate degrade, keep A, B, C. The argument:

- "Done means" needs exactly two things - run the codex device login in-flight posting URL + code to the login channel, and make `auth.json` durable. Those are `resolve_spec(target)` + moving the post before the pty spawn + the entrypoint symlink.
- The four cut pieces are most of phase 3 and carry a disproportionate share of the findings: `--rebind` carries blocker 3 (and its fix above), `--status` and the doctor row both read *cached* state and so contradict D's own rule that only a real probe decides, and the cooldown stamp + reaper carry the unspecified lock/pidfile/cooldown locations and a pid-reuse hazard.
- An orphaned poller is already bounded by codex's own 15-minute code expiry, which the design leans on elsewhere - so the reaper may be solving a problem that expires on its own.
- The [release-lag correction](#tests-39-and-40-cannot-gate-this-pr-and-the-plan-should-not-pretend-otherwise) sharpens this: the ceremony's first real exercise is on a live fleet, not in CI, which is an argument for a smaller surface on the first roll.

Against: `--rebind` is the only answer to "someone left the team" on a credential surface, and `--status` is the operator's phone-reachable check in the pending message's verify line - cutting it means editing that copy too.

**If you trim, [step 3a](#step-3a---rebind-must-not-destroy-the-credential-without-running-a-ceremony) and verification 11c go with `--rebind`.** They are written as a conditional fold for exactly this reason: they are a fix *to* `--rebind`, so they are wasted work if the flag is cut, and they are load-bearing if it survives. Nothing else in the plan depends on them.

### Q7. Land F standalone, ahead of this design?

**Also escalated, also not decided here.**

F closes a live, worker-reachable hole: any worker - including one processing a fork diff, an issue body, or a webhook payload - can run `bobi agent <name> login-bootstrap --channel slack:…:dm:<attacker>` and send the **brain's** OAuth login flow to a destination it chose (Problem §7, verified in this container). The fix is deleting the `@click.option` at `cli.py:597-599`.

The case for splitting it out: it is a ~3-line security fix on a command every published image's entrypoint invokes, and bundling it makes it wait behind Gate 1 on a six-part design that is now on its fourth review round.

The case against: it is one more PR and one more release, and the plan is coherent as a unit.

**Correction owed either way, from the round-4 review:** §F currently says *"`--channel` is dropped from the agent-group `login-bootstrap` registration"* and the Relevant-files line calls that registration *"what makes `--channel` worker-reachable"*. That is wrong about the mechanism. `cli.py:3557-3561` does `agent.add_command(main.commands["login-bootstrap"])` - the **same** click `Command` object - and `cli.py:3574` then pops the top-level entry. There is no registration-scoped option set to drop from, and no surviving top-level command that would keep the flag. Registration makes the *command* reachable; the decorator makes the *flag* exist. The only implementable change is deleting the option, which removes it everywhere - which is what F wants, but it should say so. The Relevant-files entry is corrected above; F's own wording is left for whichever PR carries it.

## Proof of work

- This spec: the Problem section is first-hand verification, and five of its findings were produced by running the real CLI or reproducing the failure rather than reasoning about it - the 401 signature, symlink write-through, `codex login status` returning 0 for a bogus key, no `auth.json` before authorization, and the codex-brain credential destruction that killed an earlier draft of change B. [Appendix A](#appendix-a-verification-transcript) is the raw transcript.
- Round 4's three shell findings were likewise reproduced rather than argued: the boot abort, the plaintext key on the volume, and the fixed guard passing all five shapes. [Appendix B](#appendix-b-round-4-reproductions) is that transcript.
- Implementation PR: **tests 1-36 plus the docker lane (37/38)**. That is what the PR can prove at merge, and the PR says so plainly.
- **Owed after the fleet roll, on the issue:** tests 39 (an agent following the baked guide, authorized from Slack, single message through all its states) and 40 (post-restart `codex exec`). These are the two that prove the *feature* rather than its parts, and they are not obtainable at merge time - see [the release-lag correction](#tests-39-and-40-cannot-gate-this-pr-and-the-plan-should-not-pretend-otherwise). #958 stays open until they are posted.

---

## Appendix A: verification transcript

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

## Appendix B: round 4 reproductions

Captured 2026-08-05. Round 4's shell findings were reproduced end to end rather than argued, and the fixed function was then run against every shape it can meet.

**Blocker 4: the codex -> claude flip aborts the boot**

The machine's team flipped from a codex brain to a claude brain and restarted. `~/.codex` is still the overlay symlink to `${DATA_DIR}/codex`, so `~/.codex/auth.json` is a regular file reached through a directory symlink - `[ -f ]` true, `[ ! -L ]` true - and the engine guard no longer fires because the engine is now `claude`.

```
$ bash boot.sh          # set -euo pipefail; codex_auth_link_volume … claude subscription
mv: '/tmp/b4repro/sim/home/.codex/auth.json' and '/tmp/b4repro/sim/data/codex/auth.json' are the same file
BOOT EXIT=1
```

`BOOT REACHED THE END` never printed. The credential survives; the **boot** does not, every boot, until someone intervenes. Isolated, `mv -f X X` exits **1**.

Note what this means for verification 29: it passes on the broken function, because the credential *does* survive. Only an assertion on the exit status and on a sentinel after the call can see this - hence 29a.

**Blocker 5: a mode flip writes a plaintext key onto the volume**

```
-- boot 1: BOBI_AUTH=subscription --
  ~/.codex/auth.json -> /…/sim5/data/codex/auth.json          # link created
-- boot 2: operator sets BOBI_AUTH=api_key, machine restarts --
  guard returned 0                                            # early return, link NOT removed
  ~/.codex/auth.json -> /…/sim5/data/codex/auth.json          # link survives on the overlay
-- what landed on the volume: --
  {"OPENAI_API_KEY": "sk-proj-PLAINTEXT-SECRET"}
  mode 600
```

`materialize_codex_api_key_auth` uses Python's `path.write_text`, which follows a symlink, so the key lands on the snapshotted volume durably. This is verbatim the failure §B claimed the guard prevents.

**The fixed function, against all five shapes**

```
1. fresh claude-brain subscription machine   exit=0  link created                     ✓
2. codex -> claude flip (blocker 4)          exit=0  "already resolves to the volume"
                                                     credential intact, BOOT_OK       ✓
3. codex brain, subscription (verif. 29)     exit=0  credential intact, BOOT_OK       ✓
4. api_key + link from a subscription boot   exit=0  link removed; volume file absent
                                                     after materialization            ✓
5. second boot, already linked               exit=0  no-op                            ✓
```

Shape 3 is why the `engine` parameter is gone: the invariant guard subsumes it, and the function never reads the engine to get case 3 right.

**Blocker 6, and the exit-2 collision**

```
$ grep -rn "credentials_exist" --include=*.py .
bobi/setup/harness.py:83:    return auth_bootstrap.credentials_exist() or _macos_keychain_has_claude()
tests/test_setup_harness.py:79:    monkeypatch.setattr(auth_bootstrap, "credentials_exist", lambda home=None: False)
…
$ python3 -c "import click; print(click.UsageError.exit_code, click.ClickException.exit_code)"
2 1                                    # click 8.4.2
```

`needs_bootstrap` has no caller under `bobi/` (tests only); `_read_until_url` does - `auth_bootstrap.py:536`, on the live claude `paste_back` path - and it re-derives `_active_spec()` at line 182.

**Blocker 1, and the delivery lag**

```
$ grep -c "chat.update" bobi/slack.py           -> 0
$ echo "$BOBI_LOGIN_CHANNEL"                    -> #bobi-eng-team
$ gh repo view moda-labs/bobi-agent  --json visibility   -> PUBLIC
$ gh repo view moda-labs/moda-agents --json visibility   -> PRIVATE
```

**The bug is still live, at review time**

```
$ codex exec -s read-only --skip-git-repo-check "reply OK" < /dev/null
ERROR: unexpected status 401 Unauthorized: Missing bearer or basic authentication in header,
  url: https://api.openai.com/v1/responses, request id: req_69d12080adc54443b00fa3027dafb8fd
```

A fresh request id, hours after Appendix A's. Round 4 was therefore **same-model** like the three before it, for the same reason.

## Notes

**Four adversarial rounds, all folded into the design rather than appended.**

*Round 1* (three lenses: correctness, scope, security/operations) found that the first draft's `--force` path would report success for a revoked credential, that its blocking lock would park seven workers inside a call the harness would kill, that its whole-directory `cp -an` / `rm -rf` migration was unsafe and re-ran every boot, that narrowing the gateway guard would let a gateway team's agent authenticate around its own gateway, that a device code was minted before the channel was ever exercised, and that the `moda-agents` companion change is the only channel that reaches this fleet's agents at all. One "verified" citation (`tests/test_gitops_c22.py`) was false.

*Round 2*, re-reviewing the revision, found that the revision had introduced worse: the narrowed single-file symlink block, run "for every engine", **destroyed the OAuth credential and created a symlink loop on every codex-brained machine** (reproduced above), still leaked a plaintext `OPENAI_API_KEY` onto the volume in api_key mode, and made the subscription sweep rename the link instead of the credential. It also found that `--force`'s sibling command `login-bootstrap --channel` is worker-reachable today, that the probe needed a timeout and a 401 signature rather than a bare exit code, that `fsutil.file_lock` cannot be taken non-blocking without a scoped change, that the "explicit Bash timeout" in the recipe was a comment that sets nothing, and that a provenance string printed in Slack is copyable and so is not a control. All of those are resolved above: B is gated to subscription-mode non-codex-brained teams, the sweep resolves the link, the probe is an `AuthProbe` with three outcomes, `file_lock` gains a parameter, the recipe uses a real `timeout`, and `--status` replaces the provenance string as the operator's actual check.

*Round 3* was the design leg (the third leg of the house spec-review gate, which the first two rounds had not covered). `gstack-plan-design-review` is `interactive: true` and this session reports `interactive`, so invoking it directly would have blocked on `AskUserQuestion` with no human attached; its mandate was run as a non-blocking lens instead. It scored the operator-facing surfaces harshly and changed five things: the command is now `bobi agent <name> auth [<tool>]` with `login-bootstrap` as a hidden alias (a published image's entrypoint pins the old name, so a rename would break rollback); `--force` is deleted as a flag that skips a check D already removed, and `--rebind` replaces it, closing the wrong-account binding that the probe-passes-exit-0 ordering had made permanent; the three separate Slack messages collapse into one message edited in place, which deletes the dead-code-in-scrollback problem rather than mitigating it; the verify instruction became a runnable `fly ssh console` line, since the previous one silently required shell access the operator does not have from a phone; and `doctor` gains a codex row, because `bobi/doctor.py` already answers this question for the brain with a real call and is the front door an operator actually uses. It also caught that the recipe referenced `$BOBI_AGENT_NAME`, which **does not exist** - the agent slot name is `$BOBI_INSTANCE`, verified.

Reading the worktree's `CLAUDE.md` during that round surfaced two more: the repo's durable-state rule says `atomic_write_text` does not preserve **symlink-ness**, which is a live hazard for change B and is now a written constraint with a test rather than a hypothetical; and `CLAUDE.md` records a standing CI blind spot (no canary exercises `auth: subscription`, because a device login blocks on a human) that this plan's fake-codex stub is the missing half of.

*Round 4* was the independent gate review, run against the post-round-3 spec and told to look where the earlier rounds had not. It returned **six blockers**, and its four spot-checked claims were all true. Everything it found was verified first-hand again before folding ([Appendix B](#appendix-b-round-4-reproductions)); nothing was taken on the review's word, and nothing was refuted.

The six defects are folded into the design rather than appended as a findings list, because a spec a review holed is not implementable until it materially changes:

1. **The one-edited-message design did not work on this fleet.** `BOBI_LOGIN_CHANNEL=#bobi-eng-team` takes `_post_login_message`'s **legacy** branch (`chat.postMessage`), and `bobi/slack.py` has no `chat.update` at all - round 3 designed against the gateway branch and verification 25a would have passed green while the real path degraded to three posts. Fixed by giving the legacy branch an edit (`bobi/slack.py` gains `update_slack_message`; `_post_login_message` returns the ref it already had and discards), and by parametrizing 25a over both branches. This also **inverted Q3**, which round 3 had closed on a premise the live deployment violates.
2. **The proof of work was circular.** Tests 39/40 need a released bobi, a released pack, a rebuilt image and a fleet roll - all post-merge - and a public PR cannot be CI-gated on a private repo's PR. Now split into what the PR proves at merge and what the issue stays open for.
3. **`--rebind` could destroy the credential without running a ceremony** (removal at step 2, cooldown exit at step 3). Folded **conditionally**, because the flag's survival is [Q6](#q6-ship-the-trimmed-v1): removal moves to step 3a after every early exit, and the credential is moved to a `.bak` that the terminal state restores.
4. **The `codex_auth_link_volume` guard tested the engine, not the invariant.** A codex -> claude flip plus a restart reaches `mv -f X X`, which exits 1 and **aborts the boot** under `set -euo pipefail` - round 2's `mv -n` -> `mv -f` fix is what turned a silent no-op into a boot-failure loop. Now guarded on `readlink -f` equality, with the `engine` parameter dropped as the wrong proxy it was.
5. **"api_key behaviour is byte-identical" was false across a mode flip**: the guard returned early without removing a surviving link and a plaintext `OPENAI_API_KEY` was written through it onto the volume. The api_key branch now removes the link, and test 30 starts **dirty** - it passed only because it started from a clean `$HOME`.
6. **`bobi/setup/harness.py:83` was a missed call site**, and its test monkeypatches the function, so change A's "required, not defaulted" rule would have crashed `bobi setup` **while the suite stayed green**. That falsifies the rule as stated: a signature cannot enforce anything through a stub. A now carries a 10-site sweep, and the two stubs get pinned with `autospec`.

Plus the cheap one: **exit 2 collided with `click.UsageError.exit_code`**, so a version-skewed recipe would have recorded "operator did not authorize" for a command that never ran. That meaning moved to 4, inconclusive split out to 5, and 2 is now unreachable by design.

**Two questions are left open for Zach and deliberately not decided here:** [Q6](#q6-ship-the-trimmed-v1) (ship the reviewer's trimmed v1 - cut `--rebind`, `--status`, the doctor row, and the cooldown + reaper; keep D) and [Q7](#q7-land-f-standalone-ahead-of-this-design) (land F standalone ahead of this design). Both are scope calls, not defects.

**Cross-model review is still owed on this spec.** All four rounds were **same-model** (Opus 5) because `codex` 401s in this container, which is the bug this plan specifies - re-confirmed live during round 4 with a fresh request id ([Appendix B](#appendix-b-round-4-reproductions)). This is the standing gap made concrete: the plan that fixes the missing second opinion could not get a second opinion. Run `codex exec` against this file once the implementation lands, and record the result on the tracking issue.
