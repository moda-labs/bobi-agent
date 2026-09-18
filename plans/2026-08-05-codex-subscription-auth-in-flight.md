# Subscription auth for CLI tools: log `codex` in once per machine, from Slack

> **Status:** Draft, awaiting Gate 1 approval from Zach. No implementation until approved.
> **Tracking issue:** moda-labs/bobi-agent#958 · **Created:** 2026-08-05 · **Rewritten to v0:** 2026-08-12 · **Re-validated:** 2026-08-21
>
> **Size:** ~26 lines across 3 files in `bobi-agent`, plus 3 lines in `moda-agents`.
> The 2026-08-05 draft was 1195 lines specifying six changes; v0 is what survived a rightsizing review.
> [What was cut, and why](#what-was-cut-and-why) itemises the difference.
>
> Every claim below was verified first-hand against `origin/main` at `ac2471e6` and inside this live container on 2026-08-21.
> [Appendix A](#appendix-a-verification-2026-08-21) is the record.
>
> The filename slug still says `in-flight`. It predates the v0 rewrite and is kept so existing links resolve.

## What changed in the 2026-08-21 re-validation

The v0 rewrite was verified against `8441de2`. Main is now `ac2471e6`, 349 files and +36776/-7106 later.
Six findings. The design is unchanged; its citations and two of its counts were not.

| # | Finding | Effect |
|---|---|---|
| F1 | The `--channel` carve-out **landed** as [#1009](https://github.com/moda-labs/bobi-agent/pull/1009) (`efd886f6`, 2026-08-12), tagged `[#958]`. `run_bootstrap` no longer takes `channel=`; the destination is `$BOBI_LOGIN_CHANNEL` only | [Landed](#landed-the---channel-fix) replaces the carve-out section; Q7 closes |
| F2 | Every `auth_bootstrap.py` line number moved: +1 above line 275 (an import), -14 below it (`_parse_conversation` was deleted in favour of `bobi.conversation.parse_conversation`) | [Relevant files](#relevant-files) remapped |
| F3 | Every `cli.py` line number moved, and `--channel` is gone. The pre-check is now at `cli.py:517` | [B](#b-bobiclipy-an-optional-target-argument) remapped |
| F4 | `package/tools/codex.md` carries the false preflight claim **twice**, at `:12` and `:47`, not once | [D](#d-two-doc-lines-in-the-team-package) is 2 lines, so `moda-agents` is 3 |
| F5 | The entrypoint has **three** `${HOME}/.codex` references, not two. The third is `BRAIN_HOME_LINK` at `:90` | [C](#c-dockerdocker-entrypointsh-codex_home-on-the-volume) names and classifies all three |
| F6 | `tests/test_auth_bootstrap.py:1336` now pins the entrypoint's bare no-argument invocation | Verification 3 names it as the regression guard |

Unchanged and re-confirmed: the bug, the 401, the durability gap, `docker/docker-entrypoint.sh` (byte-identical to `8441de2`, so every entrypoint line number still holds), `bobi/setup/harness.py:83`, and the absence of any codex preflight for this team.
Nothing became moot and nothing grew.

## Purpose

`codex` is the team's only cross-model adversarial reviewer.
It returns 401 in this container, so every review this fleet runs is single-model and records an opinion it still owes.

The login ceremony that fixes this already exists and already knows how to log `codex` in.
One line makes it unreachable, and it writes to a directory that does not survive a restart.
This plan removes those two constraints and nothing else.

## Problem

### 1. The 401 is "no credential at all", not an expired or rejected one

Reproduced live on 2026-08-21:

```
ERROR: unexpected status 401 Unauthorized: Missing bearer or basic authentication in header,
url: https://api.openai.com/v1/responses, request id: req_729985b4f4424ac6a7cda28a3e0e6f4b
```

`codex` reads auth from `$CODEX_HOME`, defaulting to `$HOME/.codex`.
Here `~/.codex/auth.json` is absent and `OPENAI_API_KEY` is unset.
`aichat` is not a fallback either: `AICHAT_PLATFORM` and `AICHAT_OPENROUTER_API_KEY` are both unset.
`Missing bearer` means no header was sent. This is not a token to refresh, it is a login that never happened.

This matches the 2026-08-12 root-cause finding, which named three stacked causes: no key, `BOBI_AUTH=subscription` blocking the api-key route, and no declared dependency.
§5 covers the third and §6 the second, including why "blocks" is precise and "deletes the OAuth credential" is not.

### 2. The ceremony already supports codex, and one line makes it unreachable

`bobi/auth_bootstrap.py` carries a complete codex spec (`_SPECS["codex"]`, `auth_bootstrap.py:103-113`): `codex login --device-auth`, a `device_poll` flow, and URL + code regexes matching codex-cli 0.144.5, which is the version installed here.

The `device_poll` branch of `run_bootstrap` is also complete.
It spawns the login, scrapes the URL and one-time code, posts both to the login channel, and waits while codex polls. Nothing is pasted back.

One line makes all of it unreachable:

```python
# bobi/auth_bootstrap.py:627
spec = _active_spec()          # reads BOBI_BRAIN, pinned to the team's brain
```

eng-team declares `brain: {kind: claude}`, and `bobi agent <name> ...` pins `BOBI_BRAIN` before any command body runs.
So `run_bootstrap` always resolves the claude spec, and there is no parameter for "which CLI do I want to authenticate".
`bobi agent eng-team login-bootstrap` therefore drives `claude auth login`, finds the brain credentials already present at `cli.py:517`, and exits.

That is the bug. Everything else here makes that resolution targetable and gives the resulting credential somewhere durable to live.

### 3. It is start-time-only

`login-bootstrap` has exactly one automated caller in the tree: `docker/docker-entrypoint.sh:575`, on the brain credential, at boot.
Nothing in the manager, runtime, supervisor, or any monitor re-invokes it.
`bobi/brain_availability.py:80` names the command in an operator remedy string, but only for the brain and only as advice.
The CLI command exists so a human on `fly ssh console` can run it.

v0 keeps it that way. See [Why v0 is human-initiated](#why-v0-is-human-initiated).

### 4. On a Claude-brained team, codex credentials have nowhere durable to live

Verified in this container on 2026-08-21:

```
/home/bobi/.codex   a real directory on `none 7.8G /`   <- container overlay, ephemeral
/data               /dev/vdc 15G, 8.8G free             <- volume, durable
/data/codex         does not exist
/home/bobi/.claude -> /data/claude                      <- the brain gets durability; codex does not
```

The entrypoint puts codex credentials on the volume only when codex is the brain (`docker-entrypoint.sh:486-532`, `BRAIN_CRED_DIR="${DATA_DIR}/codex"` at `:89`).
On a Claude brain, authenticating codex today buys one container lifetime.
That makes a once-per-machine ceremony run once per deploy, which is not shippable.

This is the one premise of the 2026-08-05 draft that has survived every re-verification unchanged.

### 5. The team's own codex doc claims a preflight that does not exist

`package/tools/codex.md` tells every eng-team agent, at `:12`:

> The CLI is baked into the eng-team image and preflighted by `agent.yaml` `requires:` (installed + authed).

and again at `:47`:

> If `codex` is missing or unauthed the preflight blocks dispatch - surface that, don't silently skip.

Both are false.
`package/agent.yaml` `requires:` lists `gh`, `gstack` and `moda-skills`, with no codex entry, and the file has no `tool_library:` key at all (zero matches).
The binary is baked into the base image, so `command -v codex` succeeds and everything downstream assumes auth.

The machinery that would have caught this exists and is real: `bobi/tool_library/codex/tool.yaml:6` carries a `success:` check that runs `codex exec -s read-only --skip-git-repo-check "reply OK"` and fails closed when no credential is present.
It has never run for this team, because the team never declared the dependency.
The first thing that notices is a review agent, mid-run, at the moment the opinion was needed.

### 6. The entrypoint does not delete an OAuth `auth.json`

The 2026-08-05 draft read as if the subscription sweep blocks the device-login path, and carried guard-scoping work to protect it.
It does not, and that work is unnecessary.

`codex_auth_uses_api_key` (`docker-entrypoint.sh:336-355`) exits 0 only for a file with a truthy `OPENAI_API_KEY` and no `tokens`, and says so in its own comment:

> API-key auth ONLY: a real OPENAI_API_KEY value AND no OAuth tokens. A codex `login --device-auth` file carries an OPENAI_API_KEY field (null) ALONGSIDE `tokens`, so a bare `"OPENAI_API_KEY" in data` misreads valid OAuth as an API-key file and wipes it every boot (re-posting a device-login each time).

A device-login credential carries `tokens` and is deliberately preserved.
The sweep blocks the api-key route only, which is not the route #958 takes.
`bobi/tool_library/codex/tool.yaml:6` gates its own api-key materialization the same way, on `[ "${BOBI_AUTH:-api_key}" != "subscription" ]`.

This is the precise form of the 2026-08-12 root cause's second item.
"`BOBI_AUTH=subscription` blocks and deletes codex auth" is true of the **api-key** file and false of an **OAuth** one.
No guard scoping is needed anywhere in this plan.

## Solution

Three code changes, two doc lines, one runbook line.

### A. Let the ceremony target a CLI tool instead of the brain

`bobi/auth_bootstrap.py`:

```python
def run_bootstrap(project_path, *, target: str | None = None, ...):
    ...
    spec = _SPECS[target] if target else _active_spec()
```

An unknown target raises a clean error listing the known keys of `_SPECS`.

Then thread `spec` into the four spec-dependent calls inside that one function, each via a new `spec` parameter defaulting to `None` and falling back to `_active_spec()`:

| Site | Today | Under A |
|---|---|---|
| `auth_bootstrap.py:630` | `credentials_exist(home)` (pre-check) | `credentials_exist(home, spec)` |
| `auth_bootstrap.py:632` | `credentials_path(home)` (log line) | `credentials_path(home, spec)` |
| `auth_bootstrap.py:660` | `spawn_login(home)` | `spawn_login(home, spec)` |
| `auth_bootstrap.py:714` | `credentials_exist(home)` (outcome check) | `credentials_exist(home, spec)` |

`_scrape_login(master, url_timeout, spec)` at `auth_bootstrap.py:687` already takes the spec and needs no change.

Every new parameter is defaulted, not required.
`credentials_exist()`, `credentials_path()`, `_spawn_login()` and `needs_bootstrap()` keep working unchanged for all three existing zero-argument callers, which are `cli.py:517`, `setup/harness.py:83` and `auth_bootstrap.py:204`.
The 2026-08-05 draft made the parameter required and paid for it with a 10-site sweep, two `autospec` stub pins and a `setup/harness.py` fix.
None of that is needed once the default is right, and it is: the only caller meaning a non-brain target is the new one, and it always passes a spec explicitly.

~12 lines.

#### A1. Refuse a tool target on a gateway team

The 2026-08-05 draft stated that the gateway guard refuses every login and that `validate_auth_mode` fatals on gateway + subscription, so gateway teams are always `api_key`.
Both were true when written and are false on main.
`522f1ff` (MOD-308, [#1002](https://github.com/moda-labs/bobi-agent/pull/1002)) narrowed the guard in `run_bootstrap` (`auth_bootstrap.py:614-625`) to allow a Claude gateway without `ANTHROPIC_AUTH_TOKEN`, and relaxed `validate_auth_mode` to permit `BOBI_AUTH=subscription` on a Claude gateway.

So on a Claude-gateway team a `target="codex"` call now passes the guard and would drive `codex login --device-auth` straight against `auth.openai.com`, routing codex traffic around the gateway's audit and spend boundary with an operator's authorization on it.
That is the hazard the original draft believed it had structurally avoided.

```python
if target and cfg.brain_is_gateway:
    raise RuntimeError(
        "subscription login for a CLI tool is not available on a gateway team: "
        "it would authenticate the tool directly against its provider, around the gateway."
    )
```

The brain path keeps MOD-308's behaviour exactly. ~3 lines.

### B. `bobi/cli.py`: an optional target argument

```python
@main.command("login-bootstrap")
@click.argument("target", required=False)
```

Passed straight through to `run_bootstrap(..., target=target)` at `cli.py:521`.

The pre-check at `cli.py:517` becomes target-aware.
It is `auth_bootstrap.credentials_exist()` with no arguments today, which resolves the claude spec and returns True on this team because the brain credential exists, so it short-circuits before `run_bootstrap` is reached.
That line is the second thing standing between an operator and a codex login.

No new command, no `--status`, no `--rebind`, no new exit codes.
`login-bootstrap` gains an argument and keeps every existing behaviour, so every already-published image's entrypoint invocation (`docker-entrypoint.sh:575`) keeps working with no alias and no rollback risk.
`tests/test_auth_bootstrap.py:1336` already asserts that bare invocation; verification 3 keeps it green.

~5 lines.

### C. `docker/docker-entrypoint.sh`: `CODEX_HOME` on the volume

```sh
mkdir -p "${DATA_DIR}/codex"
chown "${APP_USER}:${APP_USER}" "${DATA_DIR}/codex"
export CODEX_HOME="${DATA_DIR}/codex"
```

The file has exactly three `${HOME}/.codex` references. Derived with
`grep -n 'HOME}/.codex' docker/docker-entrypoint.sh`, and every hit is classified:

| Line | Reference | Under C |
|---|---|---|
| `:90` | `BRAIN_HOME_LINK="${HOME}/.codex"`, the codex-brain symlink source | **Unchanged.** It already points at `${DATA_DIR}/codex`, which is what `CODEX_HOME` will be |
| `:547` | `materialize_codex_api_key_auth "${HOME}/.codex"`, the api-key writer | **Re-pointed** |
| `:557` | `codex_dir="${HOME}/.codex"`, the subscription sweep | **Re-pointed** |

Both `:547` and `:557`, not just the writer.
Re-pointing only `:547` would leave an api-key `auth.json` on the volume that the sweep never reads, which is the exact failure the sweep exists to prevent.

No branching, and identical to today on a codex brain.
`configure_brain_paths` already sets `BRAIN_CRED_DIR="${DATA_DIR}/codex"` for every codex shape (`:89`) and links `~/.codex` to it, so both sides already resolve to the same directory there.

**Why `CODEX_HOME` and not a symlink.**
`eb90538` (MOD-310, [#1003](https://github.com/moda-labs/bobi-agent/pull/1003)) landed after the original draft and made `credentials_path()` honour a provider config dir, with `credentials_dir_env="CODEX_HOME"` on the codex spec (`auth_bootstrap.py:112`, `:176-189`).
So `codex` and `auth_bootstrap.credentials_path()` now resolve to the same file through `CODEX_HOME` with no extra code.
The original draft rejected this route because `credentials_path()` would disagree; that objection expired on 2026-08-05.

`CODEX_HOME` also deletes four hazards outright rather than guarding them: no `ELOOP` on a codex brain, no `mv -f X X` boot abort after a codex-to-claude flip, no plaintext key written through a surviving link after a mode flip, and no `atomic_write_text` converting a link back into a regular file.
Five guards in the original draft existed only to defend the symlink.

~6 lines.

#### C1. Two accepted consequences

**R1. `CODEX_HOME` relocates codex's whole state directory, not just `auth.json`.**
Verified 2026-08-21: `~/.codex` here is 91 MB, holding `sessions/`, `shell_snapshots/`, five sqlite databases with WAL and SHM files, and 49 skills under `~/.codex/skills` installed by the team's gstack build step.

Under `CODEX_HOME` codex would look for skills in `${DATA_DIR}/codex/skills` and find none.
This fails silently: the gstack `requires:` `check:` tests `$HOME/.codex/skills` by literal path and asserts those entries are absent, so it stays green either way.
One line prevents it, mirroring what the codex-brain block already does for baked skills at `docker-entrypoint.sh:489-526`:

```sh
ln -sfnT "${HOME}/.codex/skills" "${DATA_DIR}/codex/skills"
```

Sessions, sqlite state and scratch still move to the volume.
`/data` has 8.8 GB free of 15 GB, so the footprint is not a constraint, but it is a real change and is named here rather than discovered later.

**R2. In `api_key` mode the plaintext key lands on the volume.**
This was round 4's blocker 5. v0 accepts it rather than gating for it.
It was priced far above its worth: `run/.env` on that same volume already holds `GH_TOKEN`, `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET` and `LINEAR_API_KEY` in plaintext at mode 600.
eng-team is subscription mode regardless.

If Gate 1 prefers the old behaviour, one `[ "${BOBI_AUTH:-api_key}" = "subscription" ]` branch around the export restores it.
v0 omits it, because a branch is a second code path and this one buys nothing this fleet uses.

### D. Two doc lines, in the team package

Delete both false preflight claims quoted in [Problem §5](#5-the-teams-own-codex-doc-claims-a-preflight-that-does-not-exist): `:12` and `:47`.

They must land in `moda-labs/moda-agents`, at `agents/moda-eng-team/tools/codex.md`.
`bobi/tool_library/codex/guide.md` reaches zero eng-team agents: `package/agent.yaml` has no `tool_library:` key, and `docs/TOOL_LIBRARY.md:129` records that a team's own `tools/<name>.md` wins anyway, which eng-team ships.
Editing the library guide would change nothing an agent reads.

`agents/baohua/tools/codex.md` already carries the corrected shape and is the precedent to copy.
All three `moda-agents` paths were confirmed present and writable on 2026-08-21.

2 lines.

### E. One runbook line

`moda-agents/bobi-deploy/docs/CONTAINERIZED_DEPLOYMENT.md`: after a fleet roll, run `bobi agent <name> login-bootstrap codex` once and authorize from Slack.

The roll is already human-driven and already carries post-roll work.
Because the credential is durable after C and codex OAuth has no refresh-token expiry (`auth_bootstrap.py:166-171`), this is once per machine, not once per deploy.

1 line.

D and E are three lines in one small `moda-agents` PR.
It is not a blocker: the bobi-agent change is complete and usable on its own the moment a human runs the command.

## Why v0 is human-initiated

The 2026-08-05 draft's centre of gravity was an unattended worker repairing its own auth mid-run.
That is what required a probe, a non-blocking lock, a cooldown, an orphan reaper, a five-value exit taxonomy, an edited Slack message, a `doctor` row, and a recovery contract shipped into a second private repo.

The evidence does not support paying for it:

- One container, one `$HOME`. Subagents are local detached subprocesses (`bobi/subagent.py:905-911`, `start_new_session=True`), and `max_concurrent_agents: 8`. One login authorizes all eight workers, including ones already running.
- Codex OAuth has no refresh-token expiry. `subscription_credentials_status` treats a present refresh token as valid for `kind == "codex"` because the real schema has no expiry field (`auth_bootstrap.py:166-171`).
- After C the credential survives restarts and redeploys.

So the 401 window is a fresh machine before anyone has logged in.
In-flight repair automates a once-per-machine event that a human-driven fleet roll already has a human standing next to, and the fallback it replaces is the one the original draft itself called costless: *"Ignoring this costs nothing: the review runs single-model."*

Cutting in-flight also closes the plan's own open security question.
`BOBI_LOGIN_CHANNEL=#bobi-eng-team` is a human channel, so a bot posting unsolicited login codes into it trains operators past codex's own warning: *"Continue only if you started this login in Codex. If a website or another person gave you this code, cancel."*
A human-initiated login has no such surface, because the operator ran the command and knows the post is theirs.
Q3 therefore needs no posture decision from Zach; v0 does not create the exposure.

Revisit in-flight if the fleet observes repeated mid-run 401s after v0 ships.
There is no evidence today that it would fire more than once per machine.

## Scope

### In scope

1. `bobi/auth_bootstrap.py`: `run_bootstrap(target=...)`, defaulted `spec` parameters on `credentials_path` / `credentials_exist` / `_spawn_login`, the four call sites inside `run_bootstrap`, the unknown-target error, and the gateway refusal for a tool target.
2. `bobi/cli.py`: the optional `target` argument on `login-bootstrap`, passed through, and the target-aware pre-check at `:517`.
3. `docker/docker-entrypoint.sh`: `${DATA_DIR}/codex` created and owned, `CODEX_HOME` exported, `:547` and `:557` re-pointed, and the skills link.
4. Tests per [Verification](#verification).
5. `moda-agents`: two doc lines and one runbook line.

### Out of scope

- Anything in-flight or agent-triggered. See [Why v0 is human-initiated](#why-v0-is-human-initiated).
- API-key auth for codex. #522/#479 own it. C changes where the api-key file lands, not whether it is written, and §6 establishes the sweep never touched the OAuth route.
- A hard `requires:` preflight gate for codex. `bobi/tool_library/codex/tool.yaml:6` would fail closed today, blocking dispatch fleet-wide before any codex login exists, converting a degraded review into a dead fleet. D makes the doc honest instead.
- Configuring `aichat` / OpenRouter as a second fallback. Confirmed still unconfigured on 2026-08-21, and the known env-only unblock is `AICHAT_PLATFORM=openrouter` + `AICHAT_OPENROUTER_API_KEY`. It answers "a cross-model opinion exists", not "subscription auth reaches CLI tools". Worth its own ticket.
- Claude `paste_back` for a tool target. No tool needs it; codex is `device_poll`.

## Landed: the `--channel` fix

`login-bootstrap` carried a `--channel` option with no privilege gate, on a command registered on the agent group and runnable by any worker.
A worker processing a fork diff, an issue body or a webhook payload could send the brain's OAuth login flow to a destination it chose.

This landed on 2026-08-12 as [#1009](https://github.com/moda-labs/bobi-agent/pull/1009) (`efd886f6`), carved out of this plan and shipped standalone off `main` rather than waiting behind Gate 1.
`run_bootstrap` no longer accepts `channel=`; `channel_ref` reads `$BOBI_LOGIN_CHANNEL` only (`auth_bootstrap.py:641-649`), and `cli.py` has no `--channel` option.

Two consequences for this plan:

- Change B adds an argument to a decorator set that no longer has `--channel` in it. There is nothing left to avoid touching.
- `run_bootstrap`'s signature changed while this spec sat in draft. A now inserts `target` into the post-#1009 signature, which is what the [A](#a-let-the-ceremony-target-a-cli-tool-instead-of-the-brain) snippet shows.

For the record, the mechanism the original draft got wrong: `agent.add_command(main.commands["login-bootstrap"])` shares the same click `Command` object, so there was no registration-scoped option set to drop. Deleting the decorator was the only implementable change, and #1009 did exactly that.

## Verification

No frontend, so no QA phase.

**`tests/test_auth_bootstrap.py`**

1. `run_bootstrap(target="codex")` on a Claude-brained team resolves the codex spec, drives `codex login --device-auth` and checks the codex credential path, with `BOBI_BRAIN` still pinned to `claude` throughout. This is the whole feature in one test.
2. Unknown target raises, and the message lists the known `_SPECS` keys.
3. `target=None` is byte-for-byte today's behaviour, and the three existing zero-argument callers (`cli.py:517`, `setup/harness.py:83`, `needs_bootstrap`) still resolve `_active_spec()`. The existing `test_login_bootstrap_posts_only_to_the_configured_channel` (`:1336`) already pins the entrypoint's bare invocation and must stay green. This is what the defaults buy, so it is asserted rather than assumed.
4. A tool target on a gateway team raises, while the brain target keeps MOD-308's behaviour on a Claude gateway without `ANTHROPIC_AUTH_TOKEN`.
5. `_scrape_login` against the recorded 0.144.5 banner yields the URL and a well-formed code through `_ANSI_RE`. The raw banner wraps the code in colour escapes, which defeats `code_re`'s leading `\b`; the strip at `auth_bootstrap.py:249` is what makes the codex flow work at all. Asserted so nobody simplifies it away.

**Docker lane** (`-m docker`, existing `tests/integration/test_container_image.py`)

6. Subscription, Claude brain: `${DATA_DIR}/codex` exists and is app-owned, `CODEX_HOME` points at it, the skills link resolves, and the directory survives a restart.
7. Subscription, codex brain: the brain credential is intact after boot, and `CODEX_HOME` resolves to the same directory the symlink already did.

**Live, operator-verified**

8. One real `bobi agent eng-team login-bootstrap codex`, authorized from Slack, followed by a real `codex exec -s read-only` adversarial pass. Proof of work on the implementation PR.
9. Restart the machine, show `codex exec` still authenticated. This is the step that fails today.

Tests 1-5 run on every PR.
6-7 need a built image, not a released one, so they run at PR time; `test_container_image.py:339` is the only place that executes the entrypoint.
8-9 need a released `bobi`, a rebuilt image and a fleet roll, so they are post-merge, and #958 stays open until their evidence is posted on the issue.

The two repos cannot be mechanically linked: `moda-labs/bobi-agent` is public and `moda-labs/moda-agents` is private, so a public PR's checks cannot depend on a private repo's PR.
That is a discipline, not an enforcement mechanism, and is stated here as one.

## What was cut, and why

The 2026-08-05 draft specified six changes. Each row is a piece it contained that v0 does not, with what is actually lost.

| Cut | Why it existed | What is lost |
|---|---|---|
| 10-site required-parameter sweep, 2 `autospec` pins, `setup/harness.py` fix | the choice to make `spec` required rather than defaulted | nothing. There are only 4 sites inside `run_bootstrap`; verification 3 covers the rest |
| `auth.json` symlink + 5 guards (`readlink -f` invariant, api-key link removal, `mv -f` self-heal, explicit chown, `docker/codex-auth.sh`, `tests/test_codex_auth_sh.py`) | durability via a single-file symlink | nothing. `CODEX_HOME` gives durability without the mechanism, so the ELOOP, boot-abort, mode-flip and atomic-write hazards stop existing rather than being guarded |
| Guard scoping around the subscription sweep | the belief that the sweep deletes an OAuth `auth.json` | nothing. It never did; see [Problem §6](#6-the-entrypoint-does-not-delete-an-oauth-authjson) |
| `pty.openpty()` machinery, `select` loop, master-fd handling, pty-child reaper | the belief that a pty is required to see the device banner | nothing. Plain redirected stdout prints the full banner. `_spawn_login` keeps its existing pty unchanged |
| `--timeout` as a whole-command budget, 300s default | fitting inside an agent's Bash-tool budget | nothing for a human-run command. `--timeout` already exists at 600s |
| `AuthProbe` + 3-outcome classification | letting an unattended caller decide whether to page a human | a machine-readable "present but revoked" verdict. `codex exec -s read-only "reply OK"` answers it live, and no caller in v0 needs it |
| non-blocking `file_lock(blocking=)`, exit 3, cooldown stamp, orphan reaper | 8 workers self-triggering and stampeding | nothing. Only a human triggers v0, one at a time |
| `bobi/slack.py` `chat.update`, message-ref return, `_edit_login_message` | removing a dead device code from scrollback | a tidier channel. Codes expire in 15 minutes and `run_bootstrap` already posts a terminal result message |
| `doctor` codex row, `--status` | an operator's cached view of codex auth | a cached view that D's own rule says cannot be trusted anyway |
| `--rebind`, step 3a, `.rebind.bak` restore | rotating a wrong-account binding | the rotation path. Workaround: delete the credential, run the command again |
| exit taxonomy 0/1/3/4/5 | an agent's `case $?` | nothing. Only the recovery contract consumed it |
| `tool_library/codex/guide.md`, `docs/TOOL_LIBRARY.md`, the moda-agents recovery contract | teaching agents to self-serve | the recovery recipe, which v0 has no command for. Reduced to the doc lines that were always true |
| fake-codex fixture, tests 1-40, 6 phases | proportional to all of the above | proportional coverage. v0 is 5 tests plus the docker lane plus one live login |
| §F, the `--channel` hole | a real, live, unrelated security bug | nothing. It [landed standalone](#landed-the---channel-fix) as #1009, sooner than Gate 1 |

Most of these exist only for in-flight agent self-service, which v0 drops.
The rest are four adversarial review rounds compounding on one 8-line shell function that v0 does not write.

## Open questions: all resolved

The 2026-08-05 draft left seven. None is open.

| # | Question | Resolution |
|---|---|---|
| Q1 | Boot-time warm login for codex, or in-flight only? | Neither. Human-initiated, once per machine, per [E](#e-one-runbook-line). The entrypoint's brain bootstrap is untouched |
| Q2 | Who gets @-mentioned, and where? | Moot. The operator ran the command; they are already looking |
| Q3 | Is agent-triggered device login an acceptable trust boundary? | Closed by cutting in-flight, not by a posture decision. v0 posts no unsolicited codes |
| Q4 | #861: close it, or leave it? | Moot twice. #861 is CLOSED by `eb90538`, and that commit made `CODEX_HOME` a first-class supported path rather than a contested one |
| Q5 | Does the brain target get a probe too? | Moot. v0 has no probe |
| Q6 | Ship the trimmed v1? | Answered by v0, which goes further. The round-4 trim kept the probe; v0 cuts it too, because a human-run login needs no machine-readable outcome |
| Q7 | Land F standalone, ahead of this design? | Done. #1009 merged 2026-08-12; see [Landed](#landed-the---channel-fix) |

Gate 1 asks Zach for one ruling: approve v0 as specified, including R1 and R2 in [C1](#c1-two-accepted-consequences).

## Relevant files

Line numbers verified 2026-08-21 against `origin/main` at `ac2471e6`.
The v0 rewrite's numbers were correct against `8441de2` and have since shifted; F2 and F3 explain how.

- `bobi/auth_bootstrap.py`: `_ANSI_RE` 57, `_SPECS` codex entry 103-113 (with `credentials_dir_env="CODEX_HOME"` at 112), `_active_spec` 117, `subscription_credentials_status` 123 (codex has no refresh expiry, 166-171), `credentials_path` 176-189, `credentials_exist` 192-197, `needs_bootstrap` 200-204, `_spawn_login` 209, `_scrape_login` 229 (ANSI strip at 249), `run_bootstrap` 584, gateway guard 614-625, `spec = _active_spec()` **627**, pre-check 630, log line 632, spawn 660, scrape-with-spec 687, outcome check 714, the post-#1009 `channel_ref` comment 641-649.
- `bobi/cli.py`: `login-bootstrap` 497, `--timeout` 498-499, the bare `credentials_exist()` pre-check **517**, `run_bootstrap` call 521. No `--channel` option since #1009.
- `docker/docker-entrypoint.sh`: byte-identical to `8441de2`, so the v0 numbers all hold. `set -euo pipefail` 14, `APP_USER` 18, `DATA_DIR` 19, `BRAIN_CRED_DIR="${DATA_DIR}/codex"` 89, `BRAIN_HOME_LINK="${HOME}/.codex"` **90**, `materialize_codex_api_key_auth` 319-334, `codex_auth_uses_api_key` **336-355**, codex-brain block 486-532 (baked-skills relink 489-526), api-key materialization 538-551 (the `${HOME}/.codex` call at **547**), subscription sweep 553-563 (`codex_dir="${HOME}/.codex"` at **557**), brain bootstrap 568-577 (the sole automated caller at **575**).
- `bobi/setup/harness.py:83`: `credentials_exist()` with no arguments. Unchanged since `8441de2` and unchanged by v0, because the parameter is defaulted.
- `bobi/brain_availability.py:80`: names `login-bootstrap` in an operator remedy string, for the brain only. Not a caller.
- `bobi/subagent.py:905-911`: `_launch_detached`, local detached subprocesses, `start_new_session=True` at 911.
- `bobi/tool_library/codex/tool.yaml:6`: the `success:` check that would verify codex auth, and never runs for this team.
- `package/agent.yaml`: `requires:` = gh, gstack, moda-skills; no codex; no `tool_library:`; `max_concurrent_agents: 8`; the gstack `check:` reading `$HOME/.codex/skills` by literal path.
- `package/tools/codex.md:12` and `:47`: the two false preflight claims, sourced from `moda-agents:agents/moda-eng-team/tools/codex.md`.
- `docs/TOOL_LIBRARY.md:129`: a team's own `tools/<name>.md` wins over the library guide.
- `tests/test_auth_bootstrap.py:1336`: pins the entrypoint's bare no-argument invocation.
- `tests/integration/test_container_image.py:339`: the only place that executes the entrypoint; `-m docker`, excluded from normal CI.

No new files.
The original draft added `docker/codex-auth.sh`, `tests/test_codex_auth_sh.py`, a fake-codex fixture and two recorded fixtures; v0 adds none of them.

## Phases

1. **Gate 1.** Zach approves v0. Nothing below starts before it.
2. **One PR** carrying A, A1, B, C, C1 and verification 1-7. Splitting them ships a targetable command with nowhere durable to write.
3. **The `moda-agents` PR** carrying D and E. Three lines, not a blocker for phase 2.
4. **After the roll:** verification 8-9 on a real container, posted to the issue, which stays open until they are.

## Proof of work

- This spec. Every claim was verified first-hand against `origin/main` at `ac2471e6` and this live container on 2026-08-21. [Appendix A](#appendix-a-verification-2026-08-21) is the record.
- Implementation PR: verification 1-7.
- Owed after the fleet roll, on the issue: verification 8-9. These prove the feature rather than its parts, and are not obtainable at merge time.

---

## Appendix A: verification 2026-08-21

Run against `origin/main` at `ac2471e6` and inside this live eng-team container.

### Scope of the drift check

```
git diff --stat 8441de2..ac2471e6 -- bobi/auth_bootstrap.py bobi/cli.py \
  docker/docker-entrypoint.sh bobi/setup/harness.py bobi/subagent.py \
  docs/TOOL_LIBRARY.md tests/integration/test_container_image.py
```

```
 bobi/auth_bootstrap.py                    |  34 +-
 bobi/cli.py                               | 354 +++++++----
 bobi/subagent.py                          | 959 +++++++++++++++++++++---------
 docs/TOOL_LIBRARY.md                      |  34 +-
 tests/integration/test_container_image.py | 135 +++++
```

`docker/docker-entrypoint.sh` and `bobi/setup/harness.py` are absent from that output, so both are byte-identical to the SHA the v0 rewrite verified against.

Five PRs landed on 2026-08-21. Checked against this plan's surface, none touches it: #1069 (sleep cycle), #1068 (probe agent name, which is what moved `subagent.py` and `docs/TOOL_LIBRARY.md`), #1060 (MOD-290 launch-time FIM, in `bobi/runtime_guard.py`, not the entrypoint), #1070 (TEAM_DEPS bake hook, container tests only), #1061 (fleet usage over MCP).

### The 401 still reproduces

`codex exec -s read-only --skip-git-repo-check "reply OK"`, run from a git worktree:

```
warning: Falling back from WebSockets to HTTPS transport. unexpected status 401 Unauthorized:
  Missing bearer or basic authentication in header, url: wss://api.openai.com/v1/responses
ERROR: unexpected status 401 Unauthorized: Missing bearer or basic authentication in header,
  url: https://api.openai.com/v1/responses, request id: req_729985b4f4424ac6a7cda28a3e0e6f4b
```

`aichat` is still unconfigured: `AICHAT_PLATFORM`, `AICHAT_OPENROUTER_API_KEY` and `OPENROUTER_API_KEY` are all unset, and the binary exits with `No such file or directory (os error 2)` on a missing config.

### Durability is still required

```
CODEX_HOME=<unset>   BOBI_AUTH=subscription   BOBI_BRAIN=claude   OPENAI_API_KEY=<unset>
/home/bobi/.codex     drwx------  a real directory, not a symlink
/home/bobi/.claude -> /data/claude
/data/codex           does not exist
/home/bobi/.codex/auth.json   does not exist
none      7.8G  970M  6.5G  13%  /       <- overlay, ephemeral
/dev/vdc   15G  5.2G  8.8G  37%  /data   <- volume, durable
codex-cli 0.144.5
```

### What `CODEX_HOME` would relocate

```
~/.codex          91M total
~/.codex/skills   49 entries, 644K   <- installed by the team's gstack build step
~/.codex/sessions, shell_snapshots, 5 sqlite DBs + WAL/SHM
```

The v0 rewrite measured 93 MB and 50 skills on 2026-08-12. R1's conclusion is unaffected.

### The preflight claim is still false, and now doubly so

`package/agent.yaml`: `requires:` = gh, gstack, moda-skills. `grep -c tool_library package/agent.yaml` returns 0.
`grep -n 'preflight' package/tools/codex.md` returns two hits, `:12` and `:47`, which is F4.

### Overlap map

`gh issue view` on each: #860, #861, #863, #868 and #901 are all CLOSED, as the v0 rewrite recorded. #958 is OPEN. #1009 is MERGED.

### Still true from earlier rounds

- `codex login --device-auth` with plain redirected stdout and no tty prints the full banner: URL, code, and the 15-minute expiry. No pty machinery is needed.
- The scrape works because of the ANSI strip. The code is emitted wrapped in colour escapes, and `\x1b[94m` ends in a word character, which defeats `code_re`'s leading `\b`:

  ```
  raw    code_re: False | url_re: True
  clean  code_re: True  | url_re: True      # after _ANSI_RE.sub("", buf)
  ```

  `_ANSI_RE` (`:57`) and the codex `code_re` (`:111`) are both unchanged on main, so this still holds. Verification 5 pins it.
- `codex login status` returns exit 0 in 17 ms with no network call for a syntactically valid but bogus API key. It reports the shape of what is on disk, never whether it authenticates. This is why v0 adds no file-based or `login status`-based health check.
- A terminated `codex login --device-auth` leaves no `auth.json`, so presence is not accidentally significant.
- Four secrets already sit in plaintext at mode 600 in `run/.env` on the durable volume: `GH_TOKEN`, `LINEAR_API_KEY`, `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`. This is R2's pricing.
- The device-code endpoint 503'd on one of three probe attempts during the v0 rewrite, so a transient login failure is real. For a human-run command the answer is to run it again.

## Appendix B: the four premises that expired before v0

The 2026-08-05 draft was written seven days and 20+ commits before the v0 rewrite. Four of its premises had expired by then, and all four corrections still hold on `ac2471e6`.

| Draft premise | Status | Evidence |
|---|---|---|
| "the subscription sweep deletes an OAuth `auth.json`, so the guard needs scoping" | Refuted | `codex_auth_uses_api_key` requires a truthy `OPENAI_API_KEY` and no `tokens`, and its comment says a device-login file must not be wiped (`docker-entrypoint.sh:336-355`) |
| "`credentials_path()` would disagree with codex under `CODEX_HOME`, so use a symlink" | Expired | `eb90538` (MOD-310, #1003) made `credentials_path()` honour `credentials_dir_env`, which is `CODEX_HOME` for codex (`auth_bootstrap.py:112`, `:176-189`) |
| "`credentials_exist()` is a `Path.is_file()` check" | Expired | it is a structural JSON check through `subscription_credentials_status` (`auth_bootstrap.py:192-197`), same commit |
| "the gateway guard refuses every login; gateway teams are always `api_key`" | Refuted | `522f1ff` (MOD-308, #1002) narrowed both the guard (`auth_bootstrap.py:614-625`) and `validate_auth_mode`. This is why [A1](#a1-refuse-a-tool-target-on-a-gateway-team) exists |

## Notes

This document replaced a 1195-line, six-part design on 2026-08-12, at Zach's direction after a rightsizing review.
The four adversarial rounds behind that draft are not wasted; their findings are what made the cut safe:

- Round 1 found that a `--force` path would report success for a revoked credential, that a blocking lock would park seven workers, that a whole-directory migration was unsafe, and that the `moda-agents` file is the only channel reaching this fleet's agents. The last survives as [D](#d-two-doc-lines-in-the-team-package).
- Round 2 found that the narrowed symlink block destroyed the OAuth credential and created a symlink loop on every codex-brained machine, and that it leaked a plaintext key onto the volume in `api_key` mode.
- Round 3 found the operator-facing surfaces wanting and produced the edited-message design.
- Round 4 found six blockers, including a boot-abort loop (`mv -f X X` under `set -euo pipefail`), a plaintext key on the volume across a mode flip, and a missed call site that would have crashed `bobi setup` while the suite stayed green.

Rounds 2 and 4 between them found five defects in one 8-line shell function, and all five lived in the symlink.
That is the argument for v0's shape: `CODEX_HOME` is not a cheaper way to guard those hazards, it is a mechanism that does not have them.
Round 4's blocker 6 is the same story on the Python side. It exists only because the draft made the parameter required, and defaulting it deletes the defect class.

**Cross-model review is still owed on this plan.**
All four original rounds, the v0 rewrite and this re-validation were same-model (Opus 5), because `codex` 401s in this container, which is the bug this plan fixes.
Re-confirmed live on 2026-08-21 with the request id above.
Run `codex exec` against this file once v0 lands, and record the result on the tracking issue.
