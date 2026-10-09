# Ask-first `login-bootstrap`: one login primitive for the brain and for CLI tools

> **Status:** Draft, awaiting Gate 1 approval from Zach.
> No implementation until approved.
> **Tracking issue:** moda-labs/bobi-agent#958 · **Spec PR:** #959 (draft) · **Written:** 2026-10-09
>
> **Supersedes** [`plans/2026-08-05-codex-subscription-auth-in-flight.md`](2026-08-05-codex-subscription-auth-in-flight.md).
> That plan posted the device code immediately and treated the brain flow and the tool flow as separate shapes.
> Zach's 2026-10-09 decisions replace both halves of that: ask first, and one shared behaviour.
>
> **Size:** ~45 lines across 5 files in `bobi-agent`, plus ~25 lines of prompt/doc text in `moda-agents`.
>
> Every file:line below was read from a grep run against `origin/main` at `83bebe49` on 2026-10-09.
> [Appendix A](#appendix-a-verification-record-2026-10-09) is the verification record, including what was executed live.

## 1. The decisions this spec implements

Zach, Slack `#bobi-eng-team` thread `1791518816.077749`, 2026-10-09. Quoted, not relitigated.

| # | Quote | What it settles |
|---|---|---|
| **Z1** | "we cannot assume the operator always has fly ssh access, so we need to use the human communication channel (in this case slack)." | The Slack relay stays. The prior simplicity review's "cut the relay, use `fly ssh`" recommendation is rejected. |
| **Z2** | "Since it's a CLI tool, the need for auth could be discovered at any time." | The device code must not expire unread. Ask first; mint only after a human replies. |
| **Z3** | "We want to reuse the behavior for brain auth though so we dont have multiple ways of doing the same thing." | One primitive, not a brain path plus a tool path. |
| **Z4** | "I think the brain can also use ask-first. You can change the prompt to 'reply to this message in a thread when you are ready, and then I will begin the login flow'." | Ask-first is unconditional, including the brain at boot. The spec's earlier "leave it out of v1" is withdrawn. |
| **Z5** | "then you can judge any user message as 'ready'" | Any human message in the thread counts. No keyword, no allowlist. |

Standing bar: simplest practical solution, no cruft.

## 2. Problem

`codex` is baked into the eng-team image and documented as an adversarial-second-opinion tool, but it has no credential and no way to get one.

Verified live in this container on 2026-10-09:

```
$ codex exec -s read-only --skip-git-repo-check "reply OK"
ERROR: unexpected status 401 Unauthorized: Missing bearer or basic authentication in header
$ ls ~/.codex/auth.json
No such file or directory
```

Four facts make that unrecoverable today.

1. **`login-bootstrap` only ever logs in the brain.**
   It resolves its login spec from the process brain (`bobi/auth_bootstrap.py:117`, `_active_spec`), and the entrypoint invokes it bare (`docker/docker-entrypoint.sh:575`).
   On a Claude-brained team there is no way to ask it for `codex`.
2. **`codex` state is not durable.**
   `~/.codex` sits on the container overlay, not the volume.
   `CODEX_HOME` is honoured by the code (`bobi/auth_bootstrap.py:112`, `bobi/brain/codex_config.py:51-56`) but set by nothing: `grep -n 'CODEX_HOME' docker/docker-entrypoint.sh` returns no match.
3. **The device code can expire unread.**
   codex's one-time code lasts 15 minutes; `login-bootstrap --timeout` defaults to 600s (`bobi/cli.py:714-715`).
   Posting the code and then hoping a human is watching is a race, and Z2 names it.
4. **The eng-team prompt claims a preflight that does not exist.**
   `moda-labs/moda-agents:agents/moda-eng-team/tools/codex.md:12` says codex is "preflighted by `agent.yaml` `requires:` (installed + authed)" and `:47` says "the preflight blocks dispatch".
   The composed `package/agent.yaml` has no `tool_library:` key, and the upstream source removed codex from it deliberately (`agents/moda-eng-team/agent.yaml:35-38`).
   So a worker is told the tool is guaranteed authed while it 401s.

## 3. Solution

One primitive, ask-first, used by both triggers.

`login-bootstrap [<tool>]`:

1. Resolve the login channel from `$BOBI_LOGIN_CHANNEL`.
   Destination stays configuration, never a caller's argument (#1009).
2. Post the ask into that channel and keep the posted message's id: *"Reply to this message in a thread when you are ready, and then I will begin the login flow."*
3. Block until a **human** replies in that message's thread.
4. Only then spawn the login CLI, scrape the sign-in URL (and, for `device_poll`, the one-time code), and post them **into that thread**.
5. Claude `paste_back`: wait in the same thread for the code, write it into the pty.
   Codex `device_poll`: wait for the CLI to exit as the human authorizes.
6. Post the outcome into the thread and verify the credential on disk.

Two triggers, one behaviour:

- **Brain at boot.** The entrypoint's existing bare call (`docker/docker-entrypoint.sh:575`), unchanged.
- **CLI tool on demand.** The director runs `login-bootstrap codex` when a worker reports a 401.

### 3.1 The wait mechanism already exists, and already runs at boot

This was the one feasibility risk worth checking, because at boot the brain is dead and nothing is listening for a reply.
It is not a risk: `login-bootstrap` already does its own waiting today.

`_wait_for_code` (`bobi/auth_bootstrap.py:513`) subscribes to the chat topic over the event server and blocks on a queue:

- It reads the event server URL from config (`bobi/auth_bootstrap.py:522`; `event_server_url` at `bobi/config.py:485`), which is a **remote** HTTP endpoint, not an in-container process.
- It registers a listener under its own deployment name `"login-bootstrap"` (`bobi/auth_bootstrap.py:542`) and streams events through `EventServerClient` (`:558-560`).
- The Claude `paste_back` flow calls it at boot already (`bobi/auth_bootstrap.py:670`).

And the boot ordering makes the brain's absence irrelevant: `login-bootstrap` runs in entrypoint section 4 (`docker/docker-entrypoint.sh:565-577`), and the manager is not `exec`'d until section 5 (`:590-640`).
The waiter is `login-bootstrap`'s own process.

**So no new polling loop is needed.**
The task brief suggested polling Slack `conversations.replies` with the bot token as a candidate mechanism.
Pushing back with evidence: `conversations.replies` appears nowhere in `bobi/` on main (`grep -rn 'conversations.replies' --include=*.py .` matches only `tests/integration/test_channel_gateway.py:86` and `:251`, which are a stub's dispatch table, plus `docs/RELEASE_RUNBOOK.md:389`, an operator `venn` invocation).
Adding a direct Slack poll would be a second way to read a reply, which Z3 forbids, and it would work only on Slack, where the event-bus path already works on Slack, Discord and WhatsApp.
A gateway polling primitive does exist as a fallback (`channels_history`, `bobi/events/gateway.py:96`), and is rejected for the same reason: the push path is already there.

### 3.2 Change 1 - ask-first in `run_bootstrap` (both flows)

Four pieces, all in `bobi/auth_bootstrap.py`.

**(a) Return the posted message id.**
`_post_login_message` (`:382`) currently returns `None` and discards both post paths' results.
Both already carry the id: the legacy Slack path returns `chat.postMessage`'s parsed body (`bobi/slack.py:474-492`, via `_slack_api` at `:212-237`), and the gateway path's `channels_send` documents `ts` as "the posted/updated message id" (`bobi/events/gateway.py:69-70`).
Return it.

**(b) Post into a thread.**
Both paths already support it, so this is argument passing, not new transport.

- Legacy: `post_slack_message` already takes `thread_ts` (`bobi/slack.py:478`).
- Gateway: posting to a `<destination>:thread:<ts>` ref threads the message.
  The Slack channel adapter maps a parsed `threadId` straight onto `threadTs` (`event-server/core/src/channels.ts:338`), and `slack` declares `threads: true` (`:313`).
  The ref grammar is `<source>:<scope>:<chat_type>:<chat_id>[:thread:<thread_id>]` (`bobi/conversation.py:9-14`, `build_conversation` at `:32`).

**(c) One wait function, two predicates.**
Factor `_wait_for_code`'s subscribe/JOIN/`BubbleRejected`-retry body (`:513-577`) into `_wait_for_chat_event(project_path, channel, timeout, match)` and give it two callers:

- `_wait_for_reply` - matches any human text in the ask's thread, returns the inbound event's `conversation` ref.
- `_wait_for_code` - today's `_extract_code` predicate (`:479`), now also thread-scoped.

This keeps one code path for "block on a chat reply", which is the point of the refactor.

**(d) Human-only and thread-scoped filtering.**
The adapter already gives both signals, so this is two conditions, not new plumbing.

- **Human-only:** reject an event with `fields.bot_id`.
  The Slack adapter sets `fields.bot_id` for any bot-authored message (`event-server/core/src/adapters/chat-sdk-slack.ts:154`, `:168`) and already drops messages from *our own* bots (`:99`), so the new check only adds third-party bots.
- **Thread-scoped:** require the ask's thread.
  The adapter sets `fields.thread_ts` (`:166`) and builds a thread-anchored `conversation` (`:173`).
  This matters concretely: on the legacy Slack channel path `_extract_code` sets `expected_conversation = ""` (`bobi/auth_bootstrap.py:486-487`) and so matches **any** message in the login channel.
  Without a thread anchor, unrelated chatter in `#bobi-eng-team` would read as "ready".

**Per Z5 there is no keyword and no allowlist.** Any human message in the thread is "ready".

### 3.3 Change 2 - the `<tool>` target, in the cheap shape

`run_bootstrap` already seeds the process brain from config at `bobi/auth_bootstrap.py:626` (`set_process_brain_from_config(cfg)`), and every downstream helper resolves its spec from that env var.
Overriding it one line later retargets the whole flow.

```python
set_process_brain_from_config(cfg)
if target:
    os.environ[BRAIN_ENV] = target      # the tool's spec, not the brain's
spec = _active_spec()
```

**Verified, not assumed.** Executed against `/tmp/wt-958-main` at `83bebe49` with `PYTHONPATH` pinned to that worktree (`IMPORTED FROM: /tmp/wt-958-main/bobi/auth_bootstrap.py`):

```
after set_process_brain_from_config(claude):  claude /data/claude/.credentials.json
after os.environ[BRAIN_ENV]='codex':          codex  /data/codex/auth.json
```

One assignment flips `spec.kind`, `login_cmd` (`claude auth login --claudeai` -> `codex login --device-auth`), `flow` (`paste_back` -> `device_poll`), `shadow_env` (`ANTHROPIC_API_KEY` -> `OPENAI_API_KEY`) and `credentials_path`.

**This eliminates finding F11 from the validity review.**
The superseded plan threaded a `spec` argument into the `spawn_login(home)` call site at `:660`.
That site is an injection point (`spawn_login = spawn_login or _spawn_login`, `:604`), and 5 of 11 injected test fakes reach it with a `(home)`-only signature, so they would have raised `TypeError`.
The env override leaves `:660` byte-identical - confirmed in the same probe run - so no fake breaks.
`_spawn_login` re-reads `_active_spec()` internally (`:213`), so it picks the target up for free.

Two small edges:

- **Scope the gateway refusal to the brain.** The guard at `:613-624` raises for a gateway brain that is not Claude, or a Claude gateway with `ANTHROPIC_AUTH_TOKEN` set. It is an argument about the *brain's* credential path and must not block a *tool* login. Gate it on `target is None`.
- **Make `cli.py`'s pre-check target-aware.** `bobi/cli.py:733` calls `auth_bootstrap.credentials_exist()` before `run_bootstrap` (`:737`). On a Claude brain with a `codex` target it would check Claude's credential and print "already present - nothing to do". The CLI argument is added at `:713-716`.

**Do not wire `_refuse_runtime_lifecycle` into this command.**
Validity-review finding F17 floated it as cheap structural enforcement of "human-initiated only".
It is now incompatible with the design: the director runs `login-bootstrap codex`, and the director is a manager descendant, which is exactly what that guard refuses (`bobi/cli.py:471-483`, `caller_is_manager_descendant` at `bobi/service.py:303`).
It is currently wired only into `stop` and `restart` (`bobi/cli.py:1129`, `:1195`). Leave it there.
Ask-first supplies the protection instead: an injected worker firing `login-bootstrap codex` posts only "reply when you are ready", and no credential-granting link exists until a human replies.

### 3.4 Change 3 - `CODEX_HOME` on the durable volume

Without this, a login that succeeds is lost on the next roll, and the 8 workers do not share it.

New section 3c in `docker/docker-entrypoint.sh`, placed **after** the codex-brain block ends at `:532`:

```sh
# --- 3c. Codex's durable config dir for non-codex brains (#958) -------------
mkdir -p "${DATA_DIR}/codex"
chown "${APP_USER}:${APP_USER}" "${DATA_DIR}/codex"
[ -e "${DATA_DIR}/codex/skills" ] \
  || ln -sT "${HOME}/.codex/skills" "${DATA_DIR}/codex/skills"
export CODEX_HOME="${DATA_DIR}/codex"
```

The export reaches every process.
`as_app` and the final `exec` use `gosu ... env VAR=... cmd` (`:416`, `:637`), and `env` **adds** to the inherited environment rather than replacing it.
`credentials_path()` then resolves to `/data/codex/auth.json` for free, because the codex spec declares `credentials_dir_env="CODEX_HOME"` (`bobi/auth_bootstrap.py:112`).

**The placement and the guard are load-bearing. The superseded plan's line breaks boot; reproduced both ways.**
That plan proposed `ln -sfnT "${HOME}/.codex/skills" "${DATA_DIR}/codex/skills"` with no guard and no stated placement.

| Placement | Result on a codex-brained machine |
|---|---|
| After the codex-brain block | `ln: /tmp/.../data/codex/skills: cannot overwrite directory`, harness **exit 1**. The block creates that path as a real directory at `:491`. With `set -euo pipefail` (`:14`) this aborts boot. |
| Before the codex-brain block | **Symlink loop.** `:528-530` then repoints `~/.codex` at `/data/codex`, so `/data/codex/skills -> ~/.codex/skills -> /data/codex/skills`. `readlink -f` exits 1, `ls` reports "Too many levels of symbolic links", and the baked skills become unreachable. |

With the `[ -e ]` guard **and** the after-placement, both brains boot clean and the link resolves, idempotently across two boots:

```
brain=claude boot=1 exit=0  skills->/tmp/eloop958/home/.codex/skills
brain=claude boot=2 exit=0  skills->/tmp/eloop958/home/.codex/skills
brain=codex  boot=1 exit=0  skills->/tmp/eloop958/data/codex/skills
brain=codex  boot=2 exit=0  skills->/tmp/eloop958/data/codex/skills
```

The guard alone is not enough: at the before-placement it still loops.
Both are required, in writing, before anyone implements this.

Four follow-on edits the export forces:

- `docker/docker-entrypoint.sh:547` - `materialize_codex_api_key_auth "${HOME}/.codex"` -> `"${CODEX_HOME}"`.
- `docker/docker-entrypoint.sh:557` - `codex_dir="${HOME}/.codex"` -> `"${CODEX_HOME}"`, so the subscription sweep at `:558-562` reads the file codex actually uses.
- `bobi/tool_library/codex/tool.yaml` - **validity-review F12.** Its `success:` (`:6`) and `fix:` (`:8`) reference codex's home by literal path 6 times (two `~/.codex`, one `~/.codex/auth.json`, three `pathlib.Path.home()/".codex"` constructions) and `grep -n 'CODEX_HOME' bobi/tool_library/codex/tool.yaml` returns nothing. Under the export, that check would read the wrong directory, miss a real credential and fail closed, and its `fix:` would write an api-key file where codex never looks. Latent for eng-team, which declares no `tool_library:`; fail-closed for the next team that does. It is this change's own breakage, so it is in scope.
- `bobi/brain/codex_config.py:46-47` - **validity-review F16.** The comment says "The entrypoint symlinks ~/.codex at the durable volume, so writing there persists", which is true only on a codex brain. One line; this change makes it unconditionally true.

And one stale comment this change falsifies: `docker/docker-entrypoint.sh:484` still reads "codex has no config-dir override", which stopped being true when `eb90538` landed `credentials_dir_env="CODEX_HOME"`.

### 3.5 Companion change in `moda-labs/moda-agents`

Prompt and doc text only. No framework code. Not a blocker for the bobi-agent PR.

**(a) Delete the false preflight claim.**
`agents/moda-eng-team/tools/codex.md:12` and `:47`.
`agents/baohua/tools/codex.md:15` already carries the corrected shape ("**It is NOT preflighted on this team, and it may not be authenticated.**") and is the precedent to copy.

**(b) The on-demand trigger, as eng-team prompt text.**

- A worker that hits a codex 401 reports "codex needs login" and **proceeds**, disclosing the gap in its output. It does not block, retry, or fail silently.
- The **director** - and only the director - runs `login-bootstrap codex` in the background. This is a recommendation, not a certainty; see [Q2](#5-open-questions-for-zach).
- One pending ask per tool, deduped across workers. While an ask is open, workers keep disclosing the gap and no second ask is posted.

**(c) Runbook line** in `bobi-deploy/docs/CONTAINERIZED_DEPLOYMENT.md`: `codex` is logged in once per machine via `login-bootstrap codex`, and the `fly ssh console` + `codex login --device-auth` path remains the fallback for an operator who has a shell.

## 4. What was cut, and why

| Cut | Why |
|---|---|
| **Old change A** - `run_bootstrap(target=...)` threading a `spec` argument into 4 call sites | Replaced by the one-line `BRAIN_ENV` override (3.3). Same capability, no call-site churn, and it does not break the 5 injected `spawn_login` fakes that F11 found. |
| **Old change A1** - a new gateway refusal guard for a tool target | No new guard needed. The *existing* guard at `:613-624` just gets scoped to `target is None`. |
| **Old change B** - a target-aware `cli.py` surface arguing its own safety | The argument is gone, not just the prose. Ask-first means an agent-reachable invocation posts no credential link until a human replies, so the operator-habituation hazard the old plan had to argue around does not arise. The 2 lines of `cli.py` wiring stay. |
| **Post-the-code-first, for both flows** | Z2. The code can expire unread. Superseded by ask-first. |
| **"Leave ask-first out of v1 for the brain"** (the thread's own Q1) | Z4 overrode it. Withdrawn. |
| **A separate brain path and tool path** | Z3. One primitive. |
| **Slack `conversations.replies` polling** | Not needed and not wanted. The event-bus wait already exists and already runs at boot (3.1); a direct Slack poll would be a second way to do one thing, Slack-only. |
| **`channels_history` polling fallback** | Same reason. The push path works. |
| **Wiring `_refuse_runtime_lifecycle` into `login-bootstrap`** (F17) | Incompatible: it refuses manager descendants, and the director is one (3.3). |
| **A reply keyword, and a replier allowlist** | Z5: any human message counts. An allowlist would add config without adding a guarantee - `$BOBI_LOGIN_CHANNEL` is already the trust boundary (#1009). |
| **Cut the Slack relay entirely** (the simplicity review's recommendation) | Z1 rejected it: `fly ssh` access cannot be assumed. |
| **Command rename, alias, `--status`, `--rebind`, new exit codes, a `doctor` codex row, a codex `requires:` preflight gate** | Already cut before this rewrite, and still cut. The preflight gate in particular would fail closed and kill the fleet before any login exists (`bobi/tool_library/codex/tool.yaml:6`). |
| **`--timeout` changes** | None needed. It already exists at 600s (`bobi/cli.py:714-715`). See Q1 for what the timeout should *mean* at boot. |
| **~300 lines of the superseded plan** | History of a design that is now overridden. The old file is kept, marked superseded, so existing links resolve. |

## 5. Open questions for Zach

**Q1. What should boot do when nobody replies to the ask?**
This is the one question the design cannot settle by itself.
Today a non-zero `login-bootstrap` aborts boot: the call at `docker/docker-entrypoint.sh:575` is unguarded under `set -euo pipefail` (`:14`), so the machine restarts and the entrypoint runs section 4 again.
- **Option A (recommended): split by trigger.** At boot, block indefinitely on the ask - the machine has no brain credential and is useless without it, and an indefinite block leaves exactly **one** ask in Slack instead of one per restart. For a tool target, keep the bounded `--timeout` (600s) and exit cleanly, because the fleet is productive without codex.
- **Option B: bounded at boot too.** Time out, exit non-zero, let the machine restart and re-post. Simpler, one rule, but it re-posts the ask on every restart cycle until a human answers - Slack spam on exactly the surface Zach has called spam before.
- Option A costs one `if target is None` on the wait timeout. I recommend A.

**Q2. Director-only for the tool trigger, or may a worker fire it?**
Recommended: **director-only**, as prompt policy.
It keeps one pending ask per tool naturally (one director, one dedup point) and matches #1009's narrowing of a command any worker's shell can reach.
Posing it rather than deciding it, because it is a policy call, not a technical constraint: ask-first already removes the hazard that motivated director-only, so a worker-fired ask is no longer dangerous, only noisier.

**Q3. How should "one pending ask per tool" be enforced?**
Prompt policy alone is dedup by convention, and 8 concurrent workers are exactly the case where convention fails.
Recommended: enforce it in code with a non-blocking lock per target, so a second `login-bootstrap codex` while one is pending exits 0 saying an ask is already open.
`bobi/fsutil.py:162` `file_lock` is the existing primitive but is unconditionally blocking (`fcntl.LOCK_EX`, `:180`), which would queue a second caller instead of refusing it.
Cheapest correct shape: add `blocking: bool = True` to `file_lock` and pass `LOCK_NB` when false (4 lines there, 3 in `auth_bootstrap`), keeping one locking code path in the tree.
If you would rather not touch a shared primitive for this, say so and it becomes prompt-only policy with the race documented.

## 6. Scope

**In scope.**

- `bobi/auth_bootstrap.py` - ask-first for both flows, thread-anchored posting, human-only reply filter, one shared wait function, the `target` parameter, gateway guard scoped to the brain.
- `bobi/cli.py` - the optional `<tool>` argument and a target-aware pre-check.
- `docker/docker-entrypoint.sh` - section 3c, the two re-pointed writers, the stale comment at `:484`.
- `bobi/tool_library/codex/tool.yaml` - honour `CODEX_HOME` (F12).
- `bobi/brain/codex_config.py:46-47` - the stale comment (F16).
- `moda-labs/moda-agents` - the false preflight claim, the on-demand trigger prompt, the runbook line.

**Out of scope.**

- Any second login path, Slack-specific or otherwise (Z3).
- A codex `requires:`/preflight gate, a `doctor` codex row, a command rename, new flags or exit codes.
- Automatic re-login on credential expiry. codex OAuth has no refresh expiry (`bobi/auth_bootstrap.py:166-171`), so this is once per machine.
- Any other CLI tool. `aichat`, `gh` and `venn` are env-var/API-key based and already durable through `run/.env` on the same volume; `gstack` has no auth.

**Accepted and stated, not guarded.**

- `CODEX_HOME` moves codex's state (~101 MB today) to the volume. `/data` has 9.1 GB free of 15 GB.
- In `api_key` mode a plaintext key lands on the volume, alongside `GH_TOKEN`, `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET` and `LINEAR_API_KEY`, which already live there at mode 600.
- codex writes PATH-alias helper binaries into `CODEX_HOME` on every invocation, so they land on the volume. `tests/integration/test_container_image.py:275` guards root-owned entries under `/home/bobi` but not under `/data/codex`. Section 3c's `chown` covers first boot; no root-run `codex` invocation exists in the entrypoint today.

## 7. Verification plan

Unit, `tests/test_auth_bootstrap.py` (baseline today: 78 passed in 2.51s).

1. **Ask-first ordering, `device_poll`.** The ask is posted and the reply is consumed **before** `spawn_login` is called. Assert on call order; this is the Z2 guarantee and the one test that would catch a regression to post-first.
2. **Ask-first ordering, `paste_back`.** Same, for the brain flow. Z4.
3. **A bot reply is not "ready".** An event carrying `fields.bot_id` does not satisfy the wait; a following human event does.
4. **An out-of-thread human message is not "ready".** A message in the login channel with no/other `thread_ts` is ignored. This is the legacy-channel gap in 3.2(d).
5. **The device code is posted into the ask's thread**, not to the channel root.
6. **`target` retargets everything.** With a `claude` brain and `target="codex"`, the spawned command is `codex login --device-auth` and the credential path resolves under `CODEX_HOME`.
7. **No injected fake breaks.** The existing `spawn_login=` fakes with `(home)`-only signatures still bind. Pins the F11 fix.
8. **The gateway guard does not refuse a tool target.** A Claude-gateway config with `ANTHROPIC_AUTH_TOKEN` set refuses `target=None` and permits `target="codex"`.
9. **`cli.py`'s pre-check is target-aware.** With Claude credentials present and `codex` requested, it proceeds instead of printing "already present".
10. **Only one ask per target is open** (if Q3 resolves to the lock).
11. **Unchanged:** `test_login_bootstrap_posts_only_to_the_configured_channel` (`tests/test_auth_bootstrap.py:1336`) must still pass. It pins #1009's non-caller-controlled destination.

Docker lane, `tests/integration/test_container_image.py` (`-m docker`, needs a built image).

12. **Claude brain, subscription.** `/data/codex` exists and is bobi-owned, `CODEX_HOME` resolves to it in the manager's environment, the skills link resolves, and it all survives a restart.
13. **Codex brain boots clean.** Boot succeeds, the brain credential is intact, and the baked skills are reachable (`readlink -f` on `/data/codex/skills` exits 0). This is the regression pin for the abort/loop reproduced in 3.4.

Live, post-merge, owed on the issue as proof of work.

14. **One real login end to end.** `login-bootstrap codex`, a human reply in the thread, a real device code, authorize, then `codex exec -s read-only` returning 0 from a worker.
15. **Survives a restart.** Same credential after a machine restart, with no second login.

## 8. Implementation plan

Not to be started until Gate 1 is approved, and not before Q1 and Q3 are answered - both change code.

1. Tests 1-5 (ask-first, human-only, thread-scoped) against the current `run_bootstrap`, failing.
2. Change 1 (3.2), including the `_wait_for_chat_event` factoring. Tests 1-5 green, and the 78 existing tests stay green.
3. Tests 6-9, failing. Then change 2 (3.3). Test 7 is the F11 pin.
4. Change 3 (3.4): section 3c with the guard and the placement, the two re-pointed writers, `tool.yaml`, the two stale comments. Then tests 12-13.
5. Q3's lock and test 10, if Q3 resolves that way.
6. Review gate, full test run, PR.
7. The `moda-agents` companion PR (3.5).
8. Post-roll: tests 14-15 on the issue.

## Appendix A: verification record, 2026-10-09

Pinned to `origin/main` at `83bebe49`.
Spec branch `agent/958` at `295ce6fc` (the real remote head; the local `origin/agent/958` tracking ref was stale at `990f065d` because this checkout's refspec is `main`-only), merged with current main, no conflicts.
Read in a detached worktree at `/tmp/wt-958-main` cut with an explicit start-point.
The parked `run/repo` checkout (HEAD `agent/858-impl-wip`) was never grepped.
Everything that executed code ran with `GH_TOKEN=invalid`, `GITHUB_TOKEN=invalid` and a failing `gh` shim first on `PATH` (`command -v gh` -> `/tmp/neutered-bin/gh`, `gh --version` -> exit 127).

**Executed live, not read.**

| What | Result |
|---|---|
| codex 401 reproduces | `ERROR: unexpected status 401 Unauthorized: Missing bearer or basic authentication in header`; `~/.codex/auth.json` absent |
| codex honours `CODEX_HOME` | `CODEX_HOME=/tmp/ch958 codex login status` -> `Logged in using an API key`; same file with `CODEX_HOME` unset -> `Not logged in` |
| codex version | `codex-cli 0.144.5`, the version the scrape regexes were verified against |
| Brain override retargets the flow | `claude /data/claude/.credentials.json` -> `codex /data/codex/auth.json` on one assignment; `spawn_login(home)` call site unchanged |
| Superseded skills link aborts boot | after-placement: `ln: ... cannot overwrite directory`, exit 1 |
| Superseded skills link loops | before-placement: `readlink -f` exit 1, `Too many levels of symbolic links`, baked skills unreachable |
| Guard + after-placement is correct | both brains, two boots, exit 0, link resolves |
| Baseline suite | `pytest tests/test_auth_bootstrap.py -q` -> 78 passed in 2.51s |
| Live env | `BOBI_AUTH=subscription`, `BOBI_BRAIN=claude`, `BOBI_LOGIN_CHANNEL=#bobi-eng-team` (the legacy channel-name path), `CODEX_HOME` unset, `OPENAI_API_KEY` unset |

**Could not verify.**

- **An end-to-end login.** Needs a human to authorize a real device code. Each link is verified separately; the chain is not. This is verification 14.
- **The docker lane.** `-m docker`, needs a built image. No image build in this session.
- **That `/data/codex` behaves on the real volume.** Not created - that would be a live mutation during a spec step. The durability argument rests on `~/.claude -> /data/claude` on the same `/dev/vdc`.
- **Exact line counts.** Derived from the diffs this spec describes, not from an implementation.
- **A real thread reply arriving over the event bus.** The mechanism is verified by reading the code that already does it at boot and by the adapter fields it depends on; no live Slack round-trip was performed.
- **Cross-model adversarial review.** Impossible here for the reason under review: `codex` 401s and `aichat` is unconfigured (`OPENROUTER_API_KEY`/`AICHAT_PLATFORM` unset). Single-model pass, stated as one.
