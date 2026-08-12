# Subscription auth for CLI tools: log `codex` in once per machine, from Slack

> **Status:** Draft - awaiting Gate 1 approval from Zach. No implementation until approved.
> **Tracking issue:** moda-labs/bobi-agent#958 · **Created:** 2026-08-05 · **Rewritten to v0:** 2026-08-12
>
> **This is a rewrite, not an amendment.**
> The 2026-08-05 draft specified a six-part, in-flight, agent-self-service capability across four adversarial review rounds.
> A rightsizing review found it over-built, Zach agreed, and this document is what survived: **~26 lines across 3 files, plus 2 lines in `moda-agents`.**
> [What was cut, and why](#what-was-cut-and-why) is the record of the difference; nothing is silently dropped.
>
> Every claim below was verified first-hand against `origin/main` at `8441de2` and inside this live container on **2026-08-12**.
> That re-verification matters: the original draft was written against a tree seven days old and **four of its load-bearing premises had expired.**
> See [Verified 2026-08-12](#appendix-a-verification-2026-08-12).
>
> The filename slug still says `in-flight`. It predates this rewrite and is kept so the issue's existing links resolve.

## Purpose

`codex` is the team's only cross-model adversarial reviewer.
It returns 401 in worker containers, so every review this fleet runs is single-model and records an opinion it still owes.

The subscription-login ceremony that fixes this already exists and already knows how to log `codex` in.
It is unreachable for one reason, and it writes to a directory that does not survive a restart.
This plan removes those two constraints and nothing else.

## Problem

### 1. The 401 is "no credential at all", not an expired or rejected one

```
ERROR: unexpected status 401 Unauthorized: Missing bearer or basic authentication in header,
url: https://api.openai.com/v1/responses, request id: req_a74fcecafe3c426889629cc0394c5870
```

`codex` reads auth from `$CODEX_HOME`, defaulting to `$HOME/.codex`.
In this container `~/.codex/auth.json` is absent, `OPENAI_API_KEY` is unset, and `OPENROUTER_API_KEY` / `AICHAT_PLATFORM` are unset, so `aichat` is not a fallback either.
`Missing bearer` means no header was sent at all: this is not a token to refresh, it is a login that never happened.

### 2. The ceremony already supports codex, and one line makes it unreachable

`bobi/auth_bootstrap.py` carries a complete, working `codex` spec (`_SPECS["codex"]`, `auth_bootstrap.py:102-112`): `codex login --device-auth`, a `device_poll` flow, and URL + code regexes that match codex-cli 0.144.5 verbatim.

The `device_poll` branch of `run_bootstrap` is also complete: it spawns the login, scrapes the URL and one-time code, posts both to the login channel, and waits while codex polls.
Nothing is ever pasted back.

One line makes all of it unreachable:

```python
# bobi/auth_bootstrap.py:639
spec = _active_spec()          # reads BOBI_BRAIN, which is pinned to the team's brain
```

eng-team declares `brain: {kind: claude}`, and `bobi agent <name> ...` pins `BOBI_BRAIN` before any command body runs.
So `run_bootstrap` always resolves the **claude** spec, and there is no parameter for "which CLI do I want to authenticate".

`bobi agent eng-team login-bootstrap` therefore drives `claude auth login`, finds the brain credentials already present at `cli.py:506`, and exits.

**This is the whole bug.**
Everything else in this plan exists to make that one resolution targetable and to give the resulting credential somewhere durable to live.

### 3. It is start-time-only

`run_bootstrap` / `login-bootstrap` has exactly one automated caller in the tree: `docker/docker-entrypoint.sh:575`, on the brain credential, at boot.
Nothing in the manager, runtime, supervisor, or any monitor re-invokes it.
The CLI command exists so a human on `fly ssh console` can run it.

**v0 keeps it that way.** See [Why v0 is human-initiated](#why-v0-is-human-initiated).

### 4. On a Claude-brained team, codex credentials have nowhere durable to live

Verified in this container on 2026-08-12:

```
/home/bobi/.codex   a real directory on `none 7.8G /`   <- container overlay, ephemeral
/data               /dev/vdc 15G                        <- volume, durable
/data/codex         does not exist
/home/bobi/.claude -> /data/claude                      <- the brain gets durability; codex does not
```

The entrypoint puts codex credentials on the volume only when codex is the **brain** (`docker-entrypoint.sh:486-532`, `BRAIN_CRED_DIR="${DATA_DIR}/codex"` at line 89).
On a Claude brain, authenticating codex today buys exactly one container lifetime.
That turns a once-per-machine ceremony into a once-per-deploy ceremony, which is not shippable.

**This is the one premise of the original draft that survived re-verification unchanged.**

### 5. The team's own codex doc claims a preflight that does not exist

`package/tools/codex.md:12` tells every eng-team agent:

> The CLI is baked into the eng-team image and preflighted by `agent.yaml` `requires:` (installed + authed).

Verified false.
`package/agent.yaml` `requires:` lists `gh`, `gstack`, `moda-skills` and no codex entry, and the file has no `tool_library:` key at all.
The binary is baked into the base image, so `command -v codex` succeeds and everything downstream assumes auth.

The first thing that notices is a review agent, mid-run, at the moment the opinion was needed.

### 6. Correction: the entrypoint does **not** delete an OAuth `auth.json`

The 2026-08-05 draft read as if the subscription sweep blocks the device-login path, and so carried guard-scoping work to protect it.
**It does not, and that work is unnecessary.**

`codex_auth_uses_api_key` (`docker-entrypoint.sh:336-355`) exits 0 only for a file with a **truthy `OPENAI_API_KEY` and no `tokens`**, and says so in its own comment:

> API-key auth ONLY: a real OPENAI_API_KEY value AND no OAuth tokens. A codex `login --device-auth` file carries an OPENAI_API_KEY field (null) ALONGSIDE `tokens`, so a bare `"OPENAI_API_KEY" in data` misreads valid OAuth as an API-key file and wipes it every boot (re-posting a device-login each time).

A device-login credential carries `tokens` and is deliberately preserved.
The sweep blocks the **API-key** route only, which is not the route #958 takes.
**No guard scoping is needed anywhere in this plan.**

## Solution

Three code changes, one doc line, one runbook line.

### A. Let the ceremony target a CLI tool instead of the brain

`bobi/auth_bootstrap.py`:

```python
def run_bootstrap(project_path, *, target: str | None = None, ...):
    ...
    spec = _SPECS[target] if target else _active_spec()
```

An unknown target raises a clean error listing the known keys of `_SPECS`.

Then thread `spec` into the four spec-dependent calls **inside that one function**, each via a new `spec` parameter that defaults to `None` and falls back to `_active_spec()`:

| Site | Today | Under A |
|---|---|---|
| `auth_bootstrap.py:642` | `credentials_exist(home)` (pre-check) | `credentials_exist(home, spec)` |
| `auth_bootstrap.py:644` | `credentials_path(home)` (log line) | `credentials_path(home, spec)` |
| `auth_bootstrap.py:664` | `spawn_login(home)` | `spawn_login(home, spec)` |
| `auth_bootstrap.py:718` | `credentials_exist(home)` (outcome check) | `credentials_exist(home, spec)` |

`_scrape_login(master, url_timeout, spec)` at `auth_bootstrap.py:691` **already takes the spec** and needs no change.

**Defaulted, not required.**
Every new parameter defaults to `None` and falls back to `_active_spec()`, so `credentials_exist()`, `credentials_path()`, `_spawn_login()` and `needs_bootstrap()` keep working unchanged for every existing caller: `cli.py:506`, `setup/harness.py:83`, and `auth_bootstrap.py:203`.
The original draft made the parameter required and paid for it with a 10-site sweep, two `autospec` stub pins and a `setup/harness.py` fix.
None of that is needed once the default is right, and the default **is** right: the only caller that means a non-brain target is the new one, and it always passes a spec explicitly.

**~12 lines.**

#### A1. Refuse a tool target on a gateway team

**New in this rewrite, and not optional.**
The 2026-08-05 draft stated that the gateway guard "refuses every login" and that `validate_auth_mode` "already fatals on gateway + subscription, so gateway teams are always `api_key`".
**Both were true when it was written and are false on `main` today.**
`522f1ff` (MOD-308, #1002) narrowed the guard in `run_bootstrap` (`auth_bootstrap.py:626-637`) to allow a Claude gateway without `ANTHROPIC_AUTH_TOKEN`, and relaxed `validate_auth_mode` to permit `BOBI_AUTH=subscription` on a Claude gateway.

So on a Claude-gateway team a `target="codex"` call now passes the guard and would drive `codex login --device-auth` straight against `auth.openai.com`, routing codex traffic around the gateway's audit and spend boundary, with an operator's authorization on it.
That is exactly the hazard the original draft believed it had structurally avoided.

```python
if target and cfg.brain_is_gateway:
    raise RuntimeError(
        "subscription login for a CLI tool is not available on a gateway team: "
        "it would authenticate the tool directly against its provider, around the gateway."
    )
```

The brain path keeps MOD-308's behaviour exactly.
**~3 lines.**

### B. `bobi/cli.py`: an optional target argument

```python
@main.command("login-bootstrap")
@click.argument("target", required=False)
```

Passed straight through to `run_bootstrap(..., target=target)`.

The pre-check at `cli.py:506` becomes target-aware.
It is `auth_bootstrap.credentials_exist()` with no arguments today, which resolves the **claude** spec and returns True on this team because the brain credential exists, so it short-circuits before `run_bootstrap` is ever reached.
That single line is the second thing standing between an operator and a codex login.

No new command, no `--status`, no `--rebind`, no new exit codes.
`login-bootstrap` gains an argument and keeps every existing behaviour, so every already-published image's entrypoint invocation (`docker-entrypoint.sh:575`) keeps working with no alias and no rollback risk.

**~5 lines.**

### C. `docker/docker-entrypoint.sh`: `CODEX_HOME` on the volume

```sh
mkdir -p "${DATA_DIR}/codex"
chown "${APP_USER}:${APP_USER}" "${DATA_DIR}/codex"
export CODEX_HOME="${DATA_DIR}/codex"
```

and point the two existing `${HOME}/.codex` references at the same directory:

- `materialize_codex_api_key_auth "${HOME}/.codex"` (`docker-entrypoint.sh:547`)
- the subscription sweep's `codex_dir="${HOME}/.codex"` (`docker-entrypoint.sh:557`)

**Both, not just the first.**
Re-pointing only the writer and leaving the sweep reading `${HOME}/.codex` would mean an api-key `auth.json` left on the volume by an `api_key`-mode boot is never swept, which is the exact failure the sweep exists to prevent.

**No branching, and identical to today on a codex brain.**
`configure_brain_paths` already sets `BRAIN_CRED_DIR="${DATA_DIR}/codex"` for every codex shape (`docker-entrypoint.sh:89`) and links `~/.codex` to it, so both sides already resolve to the same directory there.

**Why `CODEX_HOME` and not a symlink.**
`eb90538` (MOD-310, #1003) landed after the original draft was written and made `credentials_path()` honour a provider config dir, with `credentials_dir_env="CODEX_HOME"` on the codex spec (`auth_bootstrap.py:111`, `:175-188`).
So `codex` and `auth_bootstrap.credentials_path()` now resolve to the same file through `CODEX_HOME` with **zero extra code**.
The original draft rejected this route on the grounds that `credentials_path()` would disagree; that objection expired seven days ago.

Choosing `CODEX_HOME` over an `auth.json` symlink deletes four hazards outright rather than guarding them: no `ELOOP` on a codex brain, no `mv -f X X` boot abort after a codex-to-claude flip, no plaintext key written through a surviving link after a mode flip, and no `atomic_write_text` silently converting a link back into a regular file.
Five guards in the original draft existed only to defend the symlink.

**~6 lines.**

#### C1. Two consequences, both accepted, both stated

**`CODEX_HOME` relocates codex's whole state directory, not just `auth.json`.**
Verified today: `~/.codex` here is roughly 93 MB, holding `sessions/`, `shell_snapshots/`, five sqlite databases with their WAL and SHM files, and **50 skills under `~/.codex/skills`** installed by the team's gstack build step.
(The original draft recorded 2.2 MB. That figure is stale.)

Under `CODEX_HOME` codex looks for skills in `${DATA_DIR}/codex/skills` and finds none, so those 50 skills go invisible to codex.
This fails **silently**: the gstack `requires:` `check:` tests `$HOME/.codex/skills` by literal path and asserts those entries are *absent*, so it stays green either way.
One line prevents it, mirroring what the codex-brain block already does for baked skills at `docker-entrypoint.sh:489-526`:

```sh
ln -sfnT "${HOME}/.codex/skills" "${DATA_DIR}/codex/skills"
```

Sessions, sqlite state and scratch still move to the volume.
`/data` has 8.0 GB free of 15 GB, so the footprint is not a constraint, but it is a real change and is named here rather than discovered later.

**In `api_key` mode the plaintext key lands on the volume.**
This is round 4's blocker 5, and v0 accepts it deliberately rather than gating for it.
It was priced far above its worth: `run/.env` on that same volume already holds `GH_TOKEN`, `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET` and `LINEAR_API_KEY` in plaintext at mode 600 (verified).
eng-team is subscription mode regardless.

If Gate 1 prefers the old behaviour, one `[ "${BOBI_AUTH:-api_key}" = "subscription" ]` branch around the export restores it.
v0 does not include it, because a branch is a second code path and this one buys nothing this fleet uses.

### D. One doc line, in the team package

Delete the false preflight sentence quoted in Problem §5.

**It must land in `moda-labs/moda-agents`, at `agents/moda-eng-team/tools/codex.md:12`.**
`bobi/tool_library/codex/guide.md` reaches **zero** eng-team agents: `package/agent.yaml` has no `tool_library:` key, and `docs/TOOL_LIBRARY.md:122` records that a team's own `tools/<name>.md` wins anyway, which eng-team ships.
Editing the library guide would change nothing an agent reads.

`agents/baohua/tools/codex.md` already carries the corrected shape and is the precedent to copy.

**1 line.**

### E. One runbook line

`moda-agents/bobi-deploy/docs/CONTAINERIZED_DEPLOYMENT.md`: after a fleet roll, run `bobi agent <name> login-bootstrap codex` once and authorize from Slack.

The roll is already human-driven and already carries post-roll work.
Because the credential is durable after C and codex OAuth has no refresh-token expiry (`auth_bootstrap.py:168-170`), this is once per machine, not once per deploy.

**1 line.**

D and E are two lines in one small `moda-agents` PR.
Unlike the original draft's companion PR, it is **not a blocker**: the bobi-agent change is complete and usable on its own the moment a human runs the command.

## Why v0 is human-initiated

The original draft's centre of gravity was an unattended worker repairing its own auth mid-run.
That is what required a probe, a non-blocking lock, a cooldown, an orphan reaper, a five-value exit taxonomy, an edited Slack message, a `doctor` row, and a recovery contract shipped into a second private repo.

The evidence does not support paying for it:

- **One container, one `$HOME`.** Subagents are local detached subprocesses (`bobi/subagent.py:867-873`, `start_new_session=True`), and `max_concurrent_agents: 8`. One login authorizes all eight workers, **including ones already running.**
- **Codex OAuth has no refresh-token expiry.** `subscription_credentials_status` treats a present refresh token as valid for `kind == "codex"` because the real schema has no expiry field (`auth_bootstrap.py:168-170`).
- **After C the credential survives restarts and redeploys.**

So the 401 window is a fresh machine before anyone has logged in.
In-flight repair automates a once-per-machine event that a human-driven fleet roll already has a human standing next to, and the fallback it replaces is the one the original draft itself called costless: *"Ignoring this costs nothing: the review runs single-model."*

**Cutting in-flight also closes the plan's own open security question.**
`BOBI_LOGIN_CHANNEL=#bobi-eng-team` is a human channel, so a bot posting *unsolicited* login codes into it trains operators past codex's own warning: *"Continue only if you started this login in Codex. If a website or another person gave you this code, cancel."*
A human-initiated login has no such surface: the operator ran the command, so they know the post is theirs.
Q3 does not need a posture decision from Zach any more, because v0 does not create the exposure.

Revisit in-flight if the fleet actually observes repeated mid-run 401s after v0 ships.
There is no evidence today that it would fire more than once per machine.

## Scope

### In scope

1. `bobi/auth_bootstrap.py`: `run_bootstrap(target=...)`, defaulted `spec` parameters on `credentials_path` / `credentials_exist` / `_spawn_login`, the four call sites inside `run_bootstrap`, the unknown-target error, and the gateway refusal for a tool target.
2. `bobi/cli.py`: the optional `target` argument on `login-bootstrap`, passed through, and the target-aware pre-check at line 506.
3. `docker/docker-entrypoint.sh`: `${DATA_DIR}/codex` created and owned, `CODEX_HOME` exported, the two existing `${HOME}/.codex` references re-pointed, and the skills link.
4. Tests per [Verification](#verification).
5. `moda-agents`: the doc line and the runbook line.

### Out of scope

- **The `login-bootstrap --channel` hole.** Carved out of this plan; see [Carved out](#carved-out-the---channel-hole).
- **Anything in-flight or agent-triggered.** See [Why v0 is human-initiated](#why-v0-is-human-initiated).
- **API-key auth for codex.** #522/#479 own it. C changes where the api-key file lands, not whether it is written, and §6 establishes the sweep never touched the OAuth route.
- **A hard `requires:` preflight gate for codex.** It would block dispatch fleet-wide before any codex login exists, converting a degraded review into a dead fleet. D makes the doc honest instead.
- **Configuring `aichat` / OpenRouter as a second fallback.** A real option for "a cross-model opinion exists", but not for "subscription auth reaches CLI tools". Worth its own ticket.
- **Claude `paste_back` for a tool target.** No tool needs it; codex is `device_poll`.

## Carved out: the `--channel` hole

`login-bootstrap` carries a `--channel` option (`cli.py:489-491`) with no privilege gate, on a command registered on the agent group and runnable by any worker.
A worker processing a fork diff, an issue body or a webhook payload can send the **brain's** OAuth login flow to a destination it chose.

**This is being landed standalone off `main` by a sibling worker, and is no longer part of this plan.**
It is a ~3-line security fix on a live, worker-reachable hole, unrelated to codex, and it should not wait behind Gate 1 on this design.
Do not implement it here, and do not touch `cli.py`'s option decorators in this PR beyond adding the `target` argument.

For the record, the mechanism the original draft got wrong: `agent.add_command(main.commands["login-bootstrap"])` shares the **same** click `Command` object, so there is no registration-scoped option set to drop. Deleting the decorator is the only implementable change, and it removes the flag everywhere.

## Verification

No frontend, so no QA phase.

**`tests/test_auth_bootstrap.py`**

1. `run_bootstrap(target="codex")` on a Claude-brained team resolves the codex spec, drives `codex login --device-auth` and checks the codex credential path, with `BOBI_BRAIN` still pinned to `claude` throughout. This is the whole feature in one test.
2. Unknown target raises, and the message lists the known `_SPECS` keys.
3. `target=None` is byte-for-byte today's behaviour, and the existing zero-argument callers (`cli.py:506`, `setup/harness.py:83`, `needs_bootstrap`) still resolve `_active_spec()`. This is what the defaults buy, so it is asserted rather than assumed.
4. A tool target on a gateway team raises, while the brain target keeps MOD-308's behaviour on a Claude gateway without `ANTHROPIC_AUTH_TOKEN`.
5. `_scrape_login` against the recorded 0.144.5 banner yields the URL and a well-formed code **through `_ANSI_RE`**. The raw banner wraps the code in colour escapes, which defeats `code_re`'s leading `\b`; the strip at `auth_bootstrap.py:248` is what makes the codex flow work at all. Asserted so nobody "simplifies" it away.

**Docker lane** (`-m docker`, existing `tests/integration/test_container_image.py`)

6. Subscription, Claude brain: `${DATA_DIR}/codex` exists and is app-owned, `CODEX_HOME` points at it, the skills link resolves, and the directory survives a restart.
7. Subscription, codex brain: the brain credential is intact after boot, and `CODEX_HOME` resolves to the same directory the symlink already did.

**Live, operator-verified**

8. One real `bobi agent eng-team login-bootstrap codex`, authorized from Slack, followed by a real `codex exec -s read-only` adversarial pass. Proof of work on the implementation PR.
9. Restart the machine, show `codex exec` still authenticated. This is the step that fails today.

Tests 1-5 run on every PR.
6-7 need a built image, not a released one, so they run at PR time.
8-9 need a released `bobi`, a rebuilt image and a fleet roll, so they are **post-merge** and #958 stays open until their evidence is posted on the issue.
The two repos cannot be mechanically linked either: `moda-labs/bobi-agent` is public and `moda-labs/moda-agents` is private, so a public PR's checks cannot depend on a private repo's PR.
That is a discipline, not an enforcement mechanism, and is stated here as one.

## What was cut, and why

The 2026-08-05 draft was 1195 lines specifying six changes.
Each row below is a piece it contained that v0 does not, with what is actually lost.

| Cut | Why it existed | What is lost |
|---|---|---|
| 10-site required-parameter sweep, 2 `autospec` pins, `setup/harness.py` fix | the choice to make `spec` **required** rather than defaulted | nothing. There are only 4 sites inside `run_bootstrap`; verification 3 covers the rest |
| `auth.json` symlink + 5 guards (`readlink -f` invariant, api-key link removal, `mv -f` self-heal, explicit chown, `docker/codex-auth.sh`, `tests/test_codex_auth_sh.py`) | durability via a single-file symlink | nothing. `CODEX_HOME` gives durability without the mechanism, so the ELOOP, boot-abort, mode-flip and atomic-write hazards stop existing rather than being guarded |
| Guard scoping around the subscription sweep | the belief that the sweep deletes an OAuth `auth.json` | nothing. It never did; see Problem §6 |
| `pty.openpty()` machinery, `select` loop, master-fd handling, pty-child reaper | the belief that a pty is required to see the device banner | nothing. Verified today: plain redirected stdout prints the full banner. `_spawn_login` keeps its existing pty unchanged; no new pty work is needed |
| `--timeout` as a whole-command budget, 300s default | fitting inside an agent's Bash-tool budget | nothing for a human-run command. `--timeout` already exists at 600s |
| `AuthProbe` + 3-outcome classification | letting an unattended caller decide whether to page a human | a machine-readable "present but revoked" verdict. `codex exec -s read-only "reply OK"` answers it live, and no caller in v0 needs it |
| non-blocking `file_lock(blocking=)`, exit 3, cooldown stamp, orphan reaper | 8 workers self-triggering and stampeding | nothing. Only a human triggers v0, one at a time |
| `bobi/slack.py` `chat.update`, message-ref return, `_edit_login_message` | removing a dead device code from scrollback | a tidier channel. Codes expire in 15 minutes and `run_bootstrap` already posts a terminal result message |
| `doctor` codex row, `--status` | an operator's cached view of codex auth | a cached view that D's own rule says cannot be trusted anyway |
| `--rebind`, step 3a, `.rebind.bak` restore | rotating a wrong-account binding | the rotation path. Workaround: delete the credential, run the command again |
| exit taxonomy 0/1/3/4/5 | an agent's `case $?` | nothing. Only the recovery contract consumed it |
| `tool_library/codex/guide.md`, `docs/TOOL_LIBRARY.md`, the moda-agents recovery contract | teaching agents to self-serve | the recovery recipe, which v0 has no command for. Reduced to the one doc line that was always true |
| fake-codex fixture, tests 1-40, 6 phases | proportional to all of the above | proportional coverage. v0 is 5 tests plus the docker lane plus one live login |
| §F, the `--channel` hole | a real, live, unrelated security bug | nothing: [carved out](#carved-out-the---channel-hole) and landing sooner, standalone |

Most of these exist **only** for in-flight agent self-service, which v0 drops.
The rest are four adversarial review rounds compounding on one 8-line shell function that v0 does not write.

## Open questions: all resolved

The 2026-08-05 draft left seven.
None is left open, and two of them are resolved by Zach's 2026-08-12 direction to slim the fix.

| # | Question | Resolution |
|---|---|---|
| Q1 | Boot-time warm login for codex, or in-flight only? | **Neither.** Human-initiated, once per machine, per [E](#e-one-runbook-line). The entrypoint's brain bootstrap is untouched |
| Q2 | Who gets @-mentioned, and where? | **Moot.** The operator ran the command; they are already looking |
| Q3 | Is agent-triggered device login an acceptable trust boundary? | **Closed by cutting in-flight**, not by a posture decision. v0 posts no unsolicited codes |
| Q4 | #861 - close it, or leave it? | **Moot twice.** #861 is already CLOSED by `eb90538`, and that same commit made `CODEX_HOME` a first-class supported path rather than a contested one |
| Q5 | Does the brain target get a probe too? | **Moot.** v0 has no probe |
| Q6 | Ship the trimmed v1? | **Answered by v0, which goes further.** The round-4 trim kept the probe; v0 cuts it too, because a human-run login does not need a machine-readable outcome |
| Q7 | Land F standalone, ahead of this design? | **Yes, and it is in flight.** See [Carved out](#carved-out-the---channel-hole) |

**Gate 1 therefore asks Zach for one ruling, not three:** approve v0 as specified, including the two accepted consequences in [C1](#c1-two-consequences-both-accepted-both-stated).

## Relevant files

Verified 2026-08-12 against `origin/main` at `8441de2`.
Every line number below was read first-hand today; the 2026-08-05 draft's numbers are stale after `90e1459` and the Phase 6 refactors.

- `bobi/auth_bootstrap.py` - `_SPECS` codex entry 102-112 (incl. `credentials_dir_env="CODEX_HOME"` at 111), `_ANSI_RE` 56, `_active_spec` 116, `subscription_credentials_status` 122 (codex has no refresh expiry, 168-170), `credentials_path` 175-188, `credentials_exist` 191-196, `needs_bootstrap` 199, `_spawn_login` 208, `_scrape_login` 228 (ANSI strip at 248), `run_bootstrap` 598, gateway guard 626-637, `spec = _active_spec()` **639**, pre-check 642, log line 644, spawn 664, scrape-with-spec 691, outcome check 718.
- `bobi/cli.py` - `login-bootstrap` 488, `--channel` option 489-491, `--timeout` 492, the bare `credentials_exist()` pre-check **506**, `run_bootstrap` call 510.
- `docker/docker-entrypoint.sh` - `set -euo pipefail` 14, `APP_USER` 18, `DATA_DIR` 19, `BRAIN_CRED_DIR="${DATA_DIR}/codex"` 89, `materialize_codex_api_key_auth` 319-334, `codex_auth_uses_api_key` **336-355**, codex-brain block 486-532 (baked-skills relink 489-526), api-key materialization 538-551 (the `${HOME}/.codex` call at **547**), subscription sweep 553-563 (`codex_dir="${HOME}/.codex"` at **557**), brain bootstrap 568-577.
- `bobi/setup/harness.py:83` - `credentials_exist()` with no arguments. Unchanged by v0 because the parameter is defaulted.
- `bobi/subagent.py:867-873` - `_launch_detached`, local detached subprocesses. One container, one `$HOME`.
- `package/agent.yaml` - `requires:` = gh, gstack, moda-skills; no codex; no `tool_library:`; `max_concurrent_agents: 8`; the gstack `check:` reading `$HOME/.codex/skills` by literal path.
- `package/tools/codex.md:12` - the false preflight claim, sourced from `moda-agents:agents/moda-eng-team/tools/codex.md:12`.
- `docs/TOOL_LIBRARY.md:122` - a team's own `tools/<name>.md` wins over the library guide.
- `tests/integration/test_container_image.py` - the only place the entrypoint is exercised; `-m docker`, excluded from normal CI.

No new files.
The original draft added `docker/codex-auth.sh`, `tests/test_codex_auth_sh.py`, a fake-codex fixture and two recorded fixtures; v0 adds none of them.

## Phases

1. **Gate 1.** Zach approves v0. Nothing below starts before it.
2. **One PR** carrying A, A1, B, C, C1 and verification 1-7. Splitting them ships a targetable command with nowhere durable to write.
3. **The `moda-agents` PR** carrying D and E. Two lines, not a blocker for phase 2.
4. **After the roll:** verification 8-9 on a real container, posted to the issue, which stays open until they are.

## Proof of work

- **This spec.** Every load-bearing claim was verified first-hand on 2026-08-12 against `origin/main` and this live container, and four of the 2026-08-05 draft's premises were found expired and corrected. [Appendix A](#appendix-a-verification-2026-08-12) is the record.
- **Implementation PR:** verification 1-7.
- **Owed after the fleet roll, on the issue:** verification 8-9. These prove the feature rather than its parts, and they are not obtainable at merge time.

---

## Appendix A: verification 2026-08-12

Run against `origin/main` at `8441de2` and inside this live eng-team container.
The 2026-08-05 transcript is kept below only where its evidence still stands.

### Four premises of the 2026-08-05 draft had expired

The draft was written seven days and 20+ commits before this rewrite.

| Draft premise | Status today | Evidence |
|---|---|---|
| "the subscription sweep deletes an OAuth `auth.json`, so the guard needs scoping" | **REFUTED** | `codex_auth_uses_api_key` requires truthy `OPENAI_API_KEY` **and** no `tokens`, and its comment says a device-login file must not be wiped (`docker-entrypoint.sh:336-355`) |
| "`credentials_path()` would disagree with codex under `CODEX_HOME`, so use a symlink" | **EXPIRED** | `eb90538` (MOD-310, #1003) made `credentials_path()` honour `credentials_dir_env`, which is `CODEX_HOME` for codex (`auth_bootstrap.py:111`, `:175-188`) |
| "`credentials_exist()` is a `Path.is_file()` check" | **EXPIRED** | it is a structural JSON check through `subscription_credentials_status` (`auth_bootstrap.py:191-196`), same commit |
| "the gateway guard refuses every login; gateway teams are always `api_key`" | **REFUTED** | `522f1ff` (MOD-308, #1002) narrowed both the guard (`auth_bootstrap.py:626-637`) and `validate_auth_mode`. This is why [A1](#a1-refuse-a-tool-target-on-a-gateway-team) exists |

The draft's entire **Overlap map** is also stale: #860, #861, #863, #868 and #901 are all now **CLOSED**.
It is deleted rather than updated, because v0 touches none of their code.

### A pty is not required

`codex login --device-auth`, plain redirected stdout, no tty, scratch `CODEX_HOME`, terminated before authorizing:

```
Welcome to Codex [v0.144.5]

1. Open this link in your browser and sign in to your account
   https://auth.openai.com/codex/device

2. Enter this one-time code (expires in 15 minutes)
   <code redacted; minted unpolled and long expired>

Continue only if you started this login in Codex. If a website or another person gave you this code, cancel.
```

The full banner (URL, code, 15-minute expiry) prints without a tty.

**And the reason the scrape works is `_ANSI_RE`.**
The code is emitted wrapped in colour escapes, and `\x1b[94m` ends in a word character, which defeats `code_re`'s leading `\b`:

```
raw    code_re: False | url_re: True
clean  code_re: True  | url_re: True      # after _ANSI_RE.sub("", buf)
```

That strip at `auth_bootstrap.py:248` is load-bearing for the codex flow.
Verification 5 pins it.

The device-code endpoint 503'd on one of three probe attempts during this work, so a transient login failure is real.
For a human-run command the answer is to run it again.

### Durability is genuinely required

```
/home/bobi/.codex     a real directory, not a symlink
/home/bobi/.claude -> /data/claude
/data/codex           does not exist
none      7.8G  /       <- overlay, ephemeral, where ~/.codex lives
/dev/vdc   15G  /data   <- volume, durable, 8.0G free
CODEX_HOME=<unset>   BOBI_AUTH=subscription   BOBI_BRAIN=claude
```

### What `CODEX_HOME` would relocate

```
~/.codex          ~93M total
~/.codex/skills   50 entries, 644K   <- installed by the team's gstack build step
~/.codex/sessions, shell_snapshots, 5 sqlite DBs + WAL/SHM
```

The gstack `requires:` `check:` reads `$HOME/.codex/skills` by literal path and asserts absence, so it stays green regardless.
The skills going invisible to codex is silent, which is why [C1](#c1-two-consequences-both-accepted-both-stated) links the directory.

### The api_key consequence, priced

```
-rw------- run/.env      <- on the volume, mode 600
BOBI_AUTH BOBI_BRAIN BOBI_EVENT_SERVER BOBI_FLEET BOBI_GATEWAY BOBI_INSTANCE
BOBI_LOGIN_CHANNEL GH_TOKEN LINEAR_API_KEY SLACK_BOT_TOKEN SLACK_CHANNELS
SLACK_SIGNING_SECRET
```

Four secrets already sit in plaintext on that volume at mode 600.

### Still true from 2026-08-05

- The 401 signature, reproduced again during this work with a fresh request id.
- `codex login status` returns **exit 0 in 17 ms with no network call** for a syntactically valid but bogus API key. It reports the shape of what is on disk, never whether it authenticates. This is why v0 does not add a file-based or `login status`-based health check.
- A terminated `codex login --device-auth` leaves no `auth.json`, so presence is not accidentally load-bearing.
- `run_bootstrap` / `login-bootstrap` has exactly one automated caller: `docker-entrypoint.sh:575`.
- No codex preflight for this team: `requires:` is gh, gstack, moda-skills, and there is no `tool_library:` key.

## Notes

**This document replaced a 1195-line, six-part design on 2026-08-12**, at Zach's direction after a rightsizing review, with a ~26-line v0.
[What was cut, and why](#what-was-cut-and-why) is the itemised record.

The four adversarial rounds behind the original draft are not wasted, and their findings are what made the cut safe to make:

- *Round 1* found that a `--force` path would report success for a revoked credential, that a blocking lock would park seven workers, that a whole-directory migration was unsafe, and that the `moda-agents` file is the only channel reaching this fleet's agents. The last of those survives as [D](#d-one-doc-line-in-the-team-package).
- *Round 2* found that the narrowed symlink block **destroyed the OAuth credential and created a symlink loop on every codex-brained machine**, and that it leaked a plaintext key onto the volume in `api_key` mode.
- *Round 3* found the operator-facing surfaces wanting and produced the edited-message design.
- *Round 4* found six blockers, including a boot-abort loop (`mv -f X X` under `set -euo pipefail`), a plaintext key on the volume across a mode flip, and a missed call site that would have crashed `bobi setup` while the suite stayed green.

**Rounds 2 and 4 between them found five defects in one 8-line shell function, and all five lived in the symlink.**
That is the strongest single argument for v0's shape: `CODEX_HOME` is not a cheaper way to guard those hazards, it is a mechanism that does not have them.
Round 4's blocker 6 is the same story on the Python side: it exists only because the draft made the parameter required, and defaulting it deletes the defect class.

**Cross-model review is still owed on this plan.**
All four original rounds and this rewrite were same-model (Opus 5), because `codex` 401s in this container, which is the bug this plan fixes.
Re-confirmed live during this rewrite.
Run `codex exec` against this file once v0 lands, and record the result on the tracking issue.
