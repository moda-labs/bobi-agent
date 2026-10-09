# Ask-first `login-bootstrap`: one login primitive for the brain and for CLI tools

> **Status:** Draft, awaiting Gate 1 approval from Zach.
> No implementation until approved.
> **Tracking issue:** moda-labs/bobi-agent#958 · **Spec PR:** #959 (draft) · **Written:** 2026-10-09
>
> **Supersedes** [`plans/2026-08-05-codex-subscription-auth-in-flight.md`](2026-08-05-codex-subscription-auth-in-flight.md).
> That plan posted the device code immediately and treated the brain flow and the tool flow as separate shapes.
> Zach's 2026-10-09 decisions replace both halves of that: ask first, and one shared behaviour.
>
> **Size:** ~55 lines across 5 files in `bobi-agent`, plus ~25 lines of prompt/doc text in `moda-agents`.
> Under [Q4](#5-open-questions-for-zach)'s file-level shape it is ~40 lines across 4 files.
>
> Every file:line below was read from a grep run against `origin/main` at `83bebe49` on 2026-10-09.
> [Appendix A](#appendix-a-verification-record-2026-10-09) is the verification record, including what was executed live.
>
> **Reviewed twice, both rounds committed verbatim beside this file.**
> Round 1: [`plans/reviews/2026-10-09-958-review-1.md`](reviews/2026-10-09-958-review-1.md), verdict SOUND WITH FIXES, 15 findings, 3 blockers.
> Round 2: [`plans/reviews/2026-10-09-958-review-2.md`](reviews/2026-10-09-958-review-2.md), against this revision.
> Both single-model: `codex` 401s and `aichat` is unconfigured in this container, which is the gap this spec exists to close.
>
> **On finding numbers.** Two review rounds of two different documents are cited here, and their `F<n>` numbering collides.
> A bare `F<n>` always means the round-1 review of *this* spec, linked above.
> Findings from the superseded plan's own validity review are always written out as "the superseded plan's validity review, its F<n>".

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
- It registers a listener under its own deployment name `"login-bootstrap"` (`bobi/auth_bootstrap.py:543-546`) and streams events through `EventServerClient` (`:561-563`).
- The Claude `paste_back` flow calls it at boot already (`bobi/auth_bootstrap.py:670`).

And the boot ordering makes the brain's absence irrelevant: `login-bootstrap` runs in entrypoint section 4 (`docker/docker-entrypoint.sh:565-577`), and the manager is not `exec`'d until section 5 (`:590-640`).
The waiter is `login-bootstrap`'s own process.

**The on-demand trigger also works, because delivery is fan-out, not competing consumers.**
This is the one place the two triggers genuinely differ: on demand, the manager is already running and subscribed to the same chat topic, so a competing-consumer bus would let the manager swallow the reply and starve the `login-bootstrap` listener forever.
It does not: `admittedDeploymentIds` (`event-server/core/src/core.ts:1939-1961`) accumulates every admitted subscriber into a `Set` and the runtime delivers to all of them (`docs/EVENT_SERVER.md:417-418`, "Admitted deployments are then delivered to ... locally, by direct `ws.send()`"; non-global keys "admit every indexed subscriber", `:414-415`).
So the manager and the `login-bootstrap` listener both receive the human's reply.

One consequence of that, for the companion prompt change: **the director will also see the reply**, as an ordinary thread message.
It must not re-handle it or answer it; `login-bootstrap` owns that thread until it posts its outcome.
The delivery circuit breaker is not a risk here, because it trips only on an agent's own output looping back "without a human event in between" (`docs/EVENT_SERVER.md:418-420`), and the reply is a human event.

**So no new polling loop is needed.**
The task brief suggested polling Slack `conversations.replies` with the bot token as a candidate mechanism.
Pushing back with evidence: `conversations.replies` appears nowhere in `bobi/` on main.
`grep -rn 'conversations\.replies' .` over the whole tree matches exactly two lines, `tests/integration/test_channel_gateway.py:86` and `:251`, both a test stub's dispatch table.
(`docs/RELEASE_RUNBOOK.md:389` reads `conversations_replies` with an underscore, which is a `venn` tool name in an operator recipe, not a Slack API call from this codebase.)
Adding a direct Slack poll would be a second way to read a reply, which Z3 forbids, and it would work only on Slack, where the event-bus path already works on Slack, Discord and WhatsApp.
A gateway polling primitive does exist as a fallback (`channels_history`, `bobi/events/gateway.py:95-96`), and is rejected for the same reason: the push path is already there.

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
  The ref grammar is `<source>:<scope>:<chat_type>:<chat_id>[:thread:<thread_id>]` (`bobi/conversation.py:8-14`, the grammar line at `:10`; `build_conversation` at `:32`).

**(c) One wait function, two predicates.**
Factor `_wait_for_code`'s subscribe/JOIN/`BubbleRejected`-retry body (`:513-563`, inside the function at `:513-579`) into `_wait_for_chat_event(project_path, channel, timeout, match)` and give it two callers:

- `_wait_for_reply` - matches any human text in the ask's thread, and returns the address to post into.
  On the gateway path that is the inbound event's `conversation` ref, which is already thread-anchored (`:173`).
  On the legacy Slack path there is no ref to use, so it returns the pair the legacy poster takes: the configured `legacy_slack_channel` plus the ask's `thread_ts`.
  Both post paths already accept their own form (3.2(b)), so this is a return type with two shapes, not two transports.
- `_wait_for_code` - today's `_extract_code` predicate (`:473`), now also thread-scoped.

This keeps one code path for "block on a chat reply", which is the point of the refactor.

**(d) Human-only and thread-scoped filtering.**
The adapter already gives both signals, so this is two conditions, not new plumbing.

- **Human-only:** reject an event with `fields.bot_id`.
  The Slack adapter sets `fields.bot_id` for any bot-authored message (`event-server/core/src/adapters/chat-sdk-slack.ts:154`, `:168`) and already drops messages from *our own* bots (`:99`), so the new check only adds third-party bots.
- **Thread-scoped:** require the ask's thread, and read it from **`fields.thread_ts`**.
  The field matters, so the spec names it rather than leaving it to the implementer.
  The adapter sets both `fields.thread_ts` (`:166`) and a thread-anchored `conversation` (`:173`), but only the first is usable as a filter.
  A predicate that compares the inbound `conversation` ref is a **no-op on the path this deployment runs**: with `BOBI_LOGIN_CHANNEL=#bobi-eng-team`, `_extract_code` sets `expected_conversation = ""` (`bobi/auth_bootstrap.py:477` for the `str` form, `:481-483` for a resolved `LoginChannel` with `legacy_slack_channel`), and `_conversation_matches` returns `True` unconditionally on an empty expectation (`:440-442`).
  It is barely better on the gateway path: `:445-452` deliberately lets a configured base ref match *any* `<base>:thread:<ts>` ref, which is right for "is this my channel" and wrong for "is this my thread".
  So the predicate is `fields.thread_ts == <the ask's ts>`, on both paths.

  **What this actually excludes, stated correctly.**
  It is not loose channel chatter: the adapter already drops that.
  A channel `message` with no self-mention and no `thread_ts` falls through to `return { event: null }` (`event-server/core/src/adapters/chat-sdk-slack.ts:123-124`), and a channel `message` that *does* mention the bot is dropped as a duplicate of the `app_mention` copy (`:117-118`).
  The real exposure is narrower: an unrelated **thread reply** elsewhere in the login channel (`:121-122`, emitted as `slack.thread_reply`) or an **@mention** of the bot in that channel (`:115-116`, `slack.mention`).
  Both carry `fields.channel` equal to the login channel and both satisfy today's filter.

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

**This eliminates the superseded plan's validity-review finding F11.**
That plan threaded a `spec` argument into the `spawn_login(home)` call site at `:660`.
The site is an injection point (`spawn_login = spawn_login or _spawn_login`, `:604`), and every fake that reaches it has a `(home)`-only signature, so a second positional argument would have raised `TypeError`.
Counted rather than estimated: `grep -n spawn_login tests/test_auth_bootstrap.py` returns 11 lines.
Six are `spawn_login=lambda h: None` inside `pytest.raises` guards (`:717`, `:818`, `:834`, `:849`, `:855`, `:1227`) that raise before `:660` and so never reach it at all.
Four are `def fake_spawn(home)` injections (`:621`, `:692`, `:811`, `:1209`), of which three reach `:660` and `:811`'s asserts it is never called.
One monkeypatches the module attribute, `lambda home:` (`:1289`).
So three injected fakes plus one monkeypatch actually bind at `:660`, and all four are `(home)`-only.
The env override leaves `:660` byte-identical - confirmed in the same probe run - so no fake breaks.
`_spawn_login` re-reads `_active_spec()` internally (`:213`), so it picks the target up for free.

Two small edges, and one rule that replaces what looked like two edges.

**Scope both brain-credential guards to the brain.**
`run_bootstrap` carries two refusals, and both are arguments about the *brain's* credential path, not a tool's:

- The **gateway guard** (`:614-625`) raises for a gateway brain that is not Claude, or a Claude gateway with `ANTHROPIC_AUTH_TOKEN` set. Its own message says what it is about: "this gateway authenticates with gateway credentials in the runtime .env."
- The **shadow-env guard** (`:635-639`) raises when `os.environ[spec.shadow_env]` is set, which the override makes `OPENAI_API_KEY` for a codex target.

Gate both on `target is None`:

```python
if target is None:
    ...  # gateway guard  (:614-625)
    ...  # shadow-env guard (:635-639)
```

The shadow-env guard is the one worth justifying, because leaving it in place is defensible until you read the entrypoint.
It is a Claude-shaped invariant, and the premise does not hold for codex.
`docker/docker-entrypoint.sh:535-536` states it outright: "Unlike Claude, Codex does not read OPENAI_API_KEY directly; it expects ~/.codex/auth.json."
So an ambient `OPENAI_API_KEY` does **not** shadow a codex OAuth credential on disk the way `ANTHROPIC_API_KEY` shadows Claude's; it only becomes codex auth if the entrypoint materializes it into `auth.json`, which subscription mode explicitly refuses to do (`:549-550`, "Subscription mode: leaving OPENAI_API_KEY out of Codex auth materialization").
Keeping the refusal would exclude any claude-brained team that sets `OPENAI_API_KEY` for unrelated tooling from ever logging codex in, with no credential conflict to justify it.
The existing test still passes unchanged: `tests/test_auth_bootstrap.py:1220` drives `BRAIN_ENV` directly rather than passing `target`, so `target is None` there and the refusal is still pinned.

Then **state it in "Accepted and stated"**: a codex *tool* login is permitted alongside an ambient `OPENAI_API_KEY`, and the on-disk OAuth `auth.json` wins, because codex reads only that file.
The non-clobber guard in 3.4 is what keeps that true across boots.

And the two remaining edges:

- **Make `cli.py`'s pre-check target-aware.** `bobi/cli.py:733` calls `auth_bootstrap.credentials_exist()` before `run_bootstrap` (`:737`). On a Claude brain with a `codex` target it would check Claude's credential and print "already present - nothing to do". The CLI argument is added at `:713-716`.
- **`run_bootstrap`'s own short-circuit needs no change.** `:630-633` returns `True` when the credential already exists, and it reads `credentials_path(home)` *after* the `BRAIN_ENV` override at `:626`, so it is target-aware for free. A second `login-bootstrap codex` after a successful first one is already a clean no-op. This matters for Q3.

**Do not wire `_refuse_runtime_lifecycle` into this command.**
The superseded plan's validity-review finding F17 floated it as cheap structural enforcement of "human-initiated only".
It is now incompatible with the design: the director runs `login-bootstrap codex`, and the director is a manager descendant, which is exactly what that guard refuses (`bobi/cli.py:471-483`, `caller_is_manager_descendant` at `bobi/service.py:303`).
It is currently wired only into `stop` and `restart` (`bobi/cli.py:1129`, `:1195`). Leave it there.
Ask-first supplies the protection instead: an injected worker firing `login-bootstrap codex` posts only "reply when you are ready", and no credential-granting link exists until a human replies.

### 3.4 Change 3 - `CODEX_HOME` on the durable volume

Without this, a login that succeeds is lost on the next roll, and the 8 workers do not share it.

New section 3c in `docker/docker-entrypoint.sh`, placed **between `:532` and `:538`**.
State the interval, not the floor.
"After `:532`" alone also admits a placement after the subscription sweep at `:563`, and the two follow-on edits below consume `${CODEX_HOME}` at `:547` and `:557`.
Under `set -euo pipefail` (`:14`) that is `CODEX_HOME: unbound variable` and an immediate boot abort on every machine.

```sh
# --- 3c. Codex's durable config dir, both brains (#958) ---------------------
# Each brain asserts its own shape for ${DATA_DIR}/codex/skills, because the
# two brains want that path to be a different KIND of object: a codex brain
# owns a real directory there via section 3b, and any other brain needs a link
# out to the image's own codex skills. A guard that merely skips when the path
# exists inherits whichever shape the last boot left. The export is
# unconditional so CODEX_HOME names the same path on either brain.
mkdir -p "${DATA_DIR}/codex"
chown "${APP_USER}:${APP_USER}" "${DATA_DIR}/codex"
if [ "${ENTRYPOINT_ENGINE}" != "codex" ]; then
  rm -rf "${DATA_DIR}/codex/skills"
  ln -sT "${HOME}/.codex/skills" "${DATA_DIR}/codex/skills"
fi
export CODEX_HOME="${DATA_DIR}/codex"
```

`rm -rf` on a symlink removes the link, not its target.

and one line inside the codex-brain block, before `:491`, to drop a link a previous non-codex boot may have left on the volume:

```sh
# Section 3c may have left this as a symlink into the image HOME on a previous
# non-codex boot; a codex brain needs a real directory here.
[ -L "${BRAIN_CRED_DIR}/skills" ] && rm -f "${BRAIN_CRED_DIR}/skills"
```

Both details are load-bearing, and the simplest-looking version of this aborts boot.

- **The `!= "codex"` gate, with an unconditional assertion inside it.** Without the gate, 3c's symlink and 3b's real directory fight over the same path. With the gate but a skip-if-present guard, a non-codex boot inherits the real directory a previous codex boot left and the image's own codex skills become unreachable (the matrix below).
- **The cleanup line in 3b.** Without it, a stale 3c symlink survives a brain switch, 3b writes baked links *through* it into the image HOME, and `:529`'s `rm -rf` of that HOME then closes a symlink loop.

The bare `[ -L ... ] && rm -f ...` form is safe under `set -euo pipefail`: the entrypoint runs `bash` (`#!/usr/bin/env bash`, `:1`), where a failing non-final command of an AND list does not trigger the errexit.
Verified by executing both the bare form and a mid-block copy of it under `set -euo pipefail` on bash 5.2.37; both reached the following line with `rc=0`.

The export reaches every process.
`as_app` and the final `exec` use `gosu ... env VAR=... cmd` (`:416`, `:637`), and `env` **adds** to the inherited environment rather than replacing it.
`credentials_path()` then resolves to `/data/codex/auth.json` for free, because the codex spec declares `credentials_dir_env="CODEX_HOME"` (`bobi/auth_bootstrap.py:112`).

**The placement and the guard are load-bearing. The superseded plan's line breaks boot; reproduced both ways.**
That plan proposed `ln -sfnT "${HOME}/.codex/skills" "${DATA_DIR}/codex/skills"` with no guard and no stated placement.

| Placement | Result on a codex-brained machine |
|---|---|
| After the codex-brain block | `ln: /tmp/.../data/codex/skills: cannot overwrite directory`, harness **exit 1**. The block creates that path as a real directory at `:491`. With `set -euo pipefail` (`:14`) this aborts boot. |
| Before the codex-brain block | **Symlink loop.** `:528-530` then repoints `~/.codex` at `/data/codex`, so `/data/codex/skills -> ~/.codex/skills -> /data/codex/skills`. `readlink -f` exits 1, `ls` reports "Too many levels of symbolic links", and the baked skills become unreachable. |

The guard alone is not enough: at the before-placement it still loops.

**And a guard plus the after-placement is still not enough, because the volume outlives the brain choice.**
`/data` persists across a redeploy, so a machine can boot claude and later boot codex on the same volume.
A first version of this spec proposed exactly `[ -e ] || ln -sT`, unconditional, after the codex block, and it aborts boot on that switch: boot 1 creates `/data/codex/skills` as a symlink to `~/.codex/skills`, then boot 2's codex block repoints `~/.codex` at `/data/codex` (`:527-530`) so the symlink loops, `[ -e ]` reads false, and `ln` fails "File exists".
Three candidate shapes were replayed against every boot order on one simulated volume, with a baked skill and a tool-installed one (`gstack-browse`) in the image HOME:

```
                        no guard          skip-if-present     shape above
claude, claude          OK                OK                  OK
codex,  codex           OK                OK                  OK
claude, codex           boot2 ABORT       OK                  OK
codex,  claude          OK                boot2 SKILLS LOST   OK
claude, codex, claude   boot2+boot3 ABORT boot3 SKILLS LOST    OK
codex,  claude, codex   (not run)         boot2 SKILLS LOST    OK
```

`SKILLS LOST` means `rc=0`, no log line, and `$CODEX_HOME/skills` listing only the baked skill: the tool-installed `gstack-browse` is silently unreachable to codex for the rest of that boot.
That is the shape this spec carried into round-1 review, and it is a strictly worse failure than the abort it replaced, because nothing reports it.
`OK` means `rc=0` and `readlink -f "$CODEX_HOME/skills"` resolving, with both skills listed on a non-codex boot and the baked one on a codex boot.

Both details are required, in writing, before anyone implements this.

**The cost of `rm -rf`, stated.** A brain switch discards whatever the previous brain left at `${DATA_DIR}/codex/skills`.
That content is re-derived, not durable state: a codex boot re-links every `/opt/bobi/skills` entry unconditionally (`:505-525`), and a non-codex boot re-links the image HOME.
The one thing it does not survive is a skill installed into `~/.codex/skills` at runtime *on a codex-brained machine*, where `~/.codex` is the volume (`:530`) and 3b deliberately preserves foreign entries (`:509-523`).
Nothing in the fleet does that today: `tool_library` skills are installed into the image HOME at build time, which is the population 3c links to.
The alternative is to leave a leftover directory in place, which is the skip-if-present guard the matrix shows losing the image's skills on every claude boot after a codex one.
Discarding re-derivable state on a deliberate, rare brain switch is the cheaper of the two.

Four follow-on edits the export forces:

- **`docker/docker-entrypoint.sh:547` - `materialize_codex_api_key_auth "${HOME}/.codex"` -> `"${CODEX_HOME}"`, plus a non-clobber guard.**
  The retarget on its own is not mechanical, it is destructive.
  `:547` is the non-codex-brain arm of the `api_key`-mode block (`:538`, `:546-548`), so after the retarget a claude-brained team with `BOBI_AUTH=api_key` and `OPENAI_API_KEY` set rewrites `/data/codex/auth.json` from the env var on **every boot**, overwriting a human-minted subscription credential on the volume.
  `run_bootstrap` then refuses to re-mint it while that key is set (`:635-639`), so the team is wedged with no path back.
  Leaving `:547` on `${HOME}/.codex` is not the fix either: under the export codex reads `${CODEX_HOME}/auth.json`, so the api-key file would be written where codex no longer looks and an `api_key`-mode team's codex would 401 with no credential at all - the exact failure #958 exists to close, inflicted on a different team.
  Both halves are needed: retarget the write, and refuse to overwrite an OAuth file.
  The predicate already exists and is already used for the mirror-image sweep at `:559`:

  ```sh
  else
    # Never turn OPENAI_API_KEY into Codex auth on top of a durable OAuth
    # credential on the volume (#958). The mirror of the sweep at :553-563.
    if [ -f "${CODEX_HOME}/auth.json" ] && ! codex_auth_uses_api_key "${CODEX_HOME}"; then
      log "Leaving durable Codex OAuth auth file intact; not materializing OPENAI_API_KEY"
    else
      materialize_codex_api_key_auth "${CODEX_HOME}"
    fi
  fi
  ```

  `codex_auth_uses_api_key` (`:336-355`) exits 0 only for a file with a non-empty `OPENAI_API_KEY` and no `tokens`, and exits 1 for a missing or unparseable file, so the guard reads as "a non-api-key file is present".
  Verified: `codex login --with-api-key` writes `{"auth_mode":"apikey","OPENAI_API_KEY":...}`, which that predicate classifies as an api-key file (exit 0), so a stale api-key file is still replaced rather than pinned.
- `docker/docker-entrypoint.sh:557` - `codex_dir="${HOME}/.codex"` -> `"${CODEX_HOME}"`, so the subscription sweep at `:559-562` reads the file codex actually uses.
  This edit is both required and the recovery path for the hazard above: in subscription mode it deletes an api-key `auth.json` from the volume so OAuth can be minted.
- `bobi/tool_library/codex/tool.yaml` - **superseded plan's validity review, its F12.** Its `success:` (`:6`) and `fix:` (`:8`) reference codex's home by literal path 6 times (two `~/.codex`, one `~/.codex/auth.json`, three `pathlib.Path.home()/".codex"` constructions) and `grep -n 'CODEX_HOME' bobi/tool_library/codex/tool.yaml` returns nothing. Under the export, that check would read the wrong directory, miss a real credential and fail closed. `success:` also *writes* in its `elif` branch (`mkdir -p ~/.codex` then `p.write_text(...)`), so it would additionally create an api-key file where codex never looks, not only read the wrong directory. Latent for eng-team, which declares no `tool_library:`; fail-closed for the next team that does. It is this change's own breakage, so it is in scope.
- `bobi/brain/codex_config.py:46-47` - **superseded plan's validity review, its F16.** The comment says "The entrypoint symlinks ~/.codex at the durable volume, so writing there persists", which is true only on a codex brain. One line; this change makes it unconditionally true.

And one stale comment this change falsifies: `docker/docker-entrypoint.sh:484` still reads "codex has no config-dir override", which stopped being true when `eb90538` landed `credentials_dir_env="CODEX_HOME"`.

**The export is a process-wide switch, so here is everyone who flips with it.**
`codex_home()` (`bobi/brain/codex_config.py:51-56`) reads `CODEX_HOME`, and it has five callers beyond `credentials_path()`:

| Caller | Brain-gated? | Effect on a claude brain |
|---|---|---|
| `bobi/brain/codex_config.py:181`, `:208` | n/a, internal to codex config | Config writers; only reached via `codex.py:387` below. |
| `bobi/brain/codex.py:387` | Yes, `engine == "codex"` | No change on a claude brain. |
| `bobi/brain/instructions.py:123` | Yes, `engine == "codex"` (`:120`) | No change on a claude brain. |
| `bobi/chat_history.py:317` | No, but codex-specific by construction | Reads `${CODEX_HOME}/sessions`, which is where codex now writes them. Correct, and durable rather than lost on a roll. |
| `bobi/brain/instructions.py:129-136`, `_all_brain_targets()` | **No** | It unions `instruction_targets(kind)` over `known_brain_kinds()`, so on a claude brain the cross-kind cleanup set now includes `/data/codex/AGENTS.md` instead of `~/.codex/AGENTS.md`. Assessed benign: the set exists to strip managed blocks, and the file it strips them from is now the durable one, which is the one codex reads. |

One further reader does not go through `codex_home()` at all, and is worth naming because a path grep for `codex_home` misses it:

| Caller | Brain-gated? | Effect |
|---|---|---|
| `bobi/brain_availability.py:50`, `_account_boundary("openai")` | No, but provider-gated | It hashes `os.environ.get("CODEX_HOME") or ~/.codex` into a 12-char account-boundary digest for brain-availability incident tracking. The export changes that digest once, orphaning any existing openai-provider incident history. Benign and one-time; the openai branch is only reached for an openai-provider turn, so a claude-brained team never hits it. |

That is the complete set.
`grep -rn 'CODEX_HOME' --include=*.py .` over the tree returns, outside tests, only these consumers plus `bobi/auth_bootstrap.py:112` (the spec's own `credentials_dir_env`) and three docstrings (`bobi/chat_history.py:11`, `:308`, `bobi/brain/instructions.py:13`).

### 3.5 Companion change in `moda-labs/moda-agents`

Prompt and doc text only. No framework code. Not a blocker for the bobi-agent PR.

**(a) Delete the false preflight claim.**
`agents/moda-eng-team/tools/codex.md:12` and `:47`.
`agents/baohua/tools/codex.md:15` already carries the corrected shape ("**It is NOT preflighted on this team, and it may not be authenticated.**") and is the precedent to copy.

**(b) The on-demand trigger, as eng-team prompt text.**

- A worker that hits a codex 401 reports "codex needs login" and **proceeds**, disclosing the gap in its output. It does not block, retry, or fail silently.
- The **director** - and only the director - runs `login-bootstrap codex` in the background. This is a recommendation, not a certainty; see [Q2](#5-open-questions-for-zach).
- One pending ask per tool, deduped across workers. While an ask is open, workers keep disclosing the gap and no second ask is posted.
- The director will receive the human's "ready" reply itself, because delivery is fan-out (3.1). It must not answer it or re-handle the thread; `login-bootstrap` owns that thread until it posts the outcome.

**(c) Runbook line** in `moda-labs/moda-agents:bobi-deploy/docs/CONTAINERIZED_DEPLOYMENT.md` (the standalone `moda-labs/bobi-deploy` repo is archived; the live copy lives under `moda-agents`): `codex` is logged in once per machine via `login-bootstrap codex`, and the `fly ssh console` + `codex login --device-auth` path remains the fallback for an operator who has a shell.

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
| **A new `--timeout` *flag*** | None needed. The flag already exists at 600s (`bobi/cli.py:714-715`). Q1 is about what that existing value should *mean* at boot, and every option there changes the value or its interpretation, not the surface. |
| **~300 lines of the superseded plan** | History of a design that is now overridden. The old file is kept, marked superseded, so existing links resolve. |

## 5. Open questions for Zach

**Q1. What should boot do when nobody replies to the ask?**
This is the one question the design cannot settle by itself.
Today a non-zero `login-bootstrap` aborts boot: the call at `docker/docker-entrypoint.sh:575` is unguarded under `set -euo pipefail` (`:14`), so the machine restarts and the entrypoint runs section 4 again.
- **Option A: split by trigger, block indefinitely at boot.** One ask in Slack instead of one per restart. For a tool target, keep the bounded `--timeout` (600s) and exit cleanly, because the fleet is productive without codex.
  **Its cost is not symmetric with B's, and the earlier draft did not state it.** `login-bootstrap` runs at `:575`, inside section 4. The supervisor that watches the director from outside is not started until section 5 (`:590-640`, `exec` at `:637`/`:639`). So a credential-less machine blocks forever in section 4 with nothing above it: no health endpoint, no status file, no process to restart, and no second ask. Today's restart loop is at least visible in `fly logs` and self-heals if the credential arrives by another route.
- **Option B: bounded at boot too.** Time out, exit non-zero, let the machine restart and re-post. Simpler, one rule, but it re-posts the ask on every restart cycle until a human answers - Slack spam on exactly the surface Zach has called spam before.
- **Option C (recommended): bounded at boot with a long timeout, re-posting into the existing thread.** Block for hours rather than 600s, and on timeout exit non-zero as today, but have the next boot reply *into the ask's existing thread* instead of opening a new one. One ask in Slack, the machine still cycles, and it stays observable. It needs somewhere to remember the ask's `ts` across boots, and the durable volume the rest of this spec already introduces is that place (`${CODEX_HOME}` for a tool, the brain credential dir for the brain).
- A and B each cost about one `if target is None` on the wait timeout. C costs that plus persisting one string and a "reply into it if present" branch on the post. I recommend **C**, and B over A, because an indefinitely silent machine with no watchdog is a worse failure than a noisy one.

**Q2. Director-only for the tool trigger, or may a worker fire it?**
Recommended: **director-only**, as prompt policy.
It keeps one pending ask per tool naturally (one director, one dedup point) and matches #1009's narrowing of a command any worker's shell can reach.
Posing it rather than deciding it, because it is a policy call, not a technical constraint: ask-first already removes the hazard that motivated director-only, so a worker-fired ask is no longer dangerous, only noisier.

**Q3. How should "one pending ask per tool" be enforced?**
Answered within the spec rather than asked, because the case for code enforcement did not survive being written out.
**Recommendation: prompt-only policy, with the residual race documented. No code.**

The earlier draft recommended a non-blocking lock per target, adding `blocking: bool = True` to `file_lock` (`bobi/fsutil.py:162`, unconditionally `fcntl.LOCK_EX` at `:180`) and passing `LOCK_NB`.
Three reasons that is the wrong trade:

- **Q2 already assigns the invariant to the director**, and for exactly this reason: one director is one dedup point. A lock gives one invariant two enforcers. If the policy holds the lock is dead code; if it does not, the policy line is what needs fixing.
- **`run_bootstrap` already short-circuits.** `:630-633` returns `True` when the credential exists, read after the `BRAIN_ENV` override (3.3), so a second `login-bootstrap codex` *after* the first succeeds is already a clean no-op without any lock.
- **The residual race is cosmetic.** The only uncovered window is two asks posted before either completes: duplicate Slack messages, not a wrong credential or a corrupt file. Changing the semantics of a tree-wide primitive for message tidiness inverts the cost.

If dedup later proves necessary, the cheap local shape is a pidfile in the agent's run dir, not a new mode on `file_lock`.
Flagged here rather than buried because the earlier draft recommended the opposite; say so if you want the lock anyway.

**Q4. Should Change 3 move codex's whole config dir, or just the credential file?**
This is new in round 2, and it is the one question that changes the size of this spec.
Round-1 review raised it as unverifiable; it is now verified, and the answer points the other way from what 3.4 is written to do.

3.4 moves the **directory** (`export CODEX_HOME=/data/codex`).
That is what creates the `/data/codex/skills` object, which is the sole source of both round-1 blockers, and what redirects the five other `codex_home()` consumers.
The alternative moves only the **file**, with no env var at all:

```sh
# --- 3c. Durable codex credential for non-codex brains (#958) --------------
# A codex BRAIN already has this: section 3b points all of ~/.codex at the
# volume. Any other brain needs just the credential to survive a roll.
if [ "${ENTRYPOINT_ENGINE}" != "codex" ]; then
  mkdir -p "${DATA_DIR}/codex" "${HOME}/.codex"
  chown "${APP_USER}:${APP_USER}" "${DATA_DIR}/codex"
  ln -sfnT "${DATA_DIR}/codex/auth.json" "${HOME}/.codex/auth.json"
fi
```

**The risk that ruled this out before is now measured, and it does not hold.**
A file-level symlink only works if codex writes `auth.json` in place rather than replacing it by `rename(2)`, which would delete the link and leave the credential on the ephemeral overlay.
Executed against the baked `codex-cli 0.144.5`:

| What | Result |
|---|---|
| Write over a symlink whose target exists | `codex login --with-api-key` -> `Successfully logged in`; `~/.codex/auth.json` **still a symlink**; the new content landed in the durable target |
| Write through a **dangling** symlink | durable target created at mode `600`, link intact |
| Read through a dangling symlink | `codex login status` -> `Not logged in`, no crash |

Writes are in place. The symlink survives both.

**What that deletes from this spec.** Both round-1 blockers on the skills link (no `/data/codex/skills` object exists), the `tool.yaml` change (`~/.codex/auth.json` is still the right literal path), the `codex_home()` blast radius in 3.4, and two of the three "accepted and stated" items in section 6 (codex's ~101 MB of state and its PATH-alias helper binaries both stay on the overlay, where they are today).

**What it does not simplify, stated plainly.** Three things survive unchanged:

- The non-clobber guard on the api-key materialization is still required. `:547` writes to `${HOME}/.codex`, which now *is* the durable file through the link, so the clobber hazard is identical.
- The sweep at `:557` still has to be re-pointed, for a new reason: `rm -f "${HOME}/.codex/auth.json"` would delete the **link** and leave the api-key file on the volume, where the next boot's `ln -sfnT` would re-expose it. It must target `${DATA_DIR}/codex` on a non-codex brain.
- One new line is needed that the directory shape got for free: `materialize_codex_api_key_auth` runs as root and ends with `chown -R` on its argument, and `chown -R` does not follow the `auth.json` symlink, so a file created through the link on first boot stays root-owned. The chown must name the resolved path.

**Recommendation: take the file-level symlink.** It removes both blockers rather than fixing them, removes a scope item and two accepted risks, and costs one line.
The argument for the directory shape is house precedent (`CLAUDE_CONFIG_DIR`, and 3b's own `~/.codex` -> volume on a codex brain) plus durable codex `sessions` as a side benefit, which nothing asks for.
Precedent is a real consideration and it is why 3.4 is still written out in full below rather than replaced (section 3.4, above): if you prefer the directory shape for consistency, 3.4 as written is correct and tested.
But the simplest practical bar points at the file.

## 6. Scope

**In scope.**

- `bobi/auth_bootstrap.py` - ask-first for both flows, thread-anchored posting, human-only reply filter, one shared wait function, the `target` parameter, and **both** brain-credential guards scoped to the brain (gateway `:614-625` and shadow-env `:635-639`).
- `bobi/cli.py` - the optional `<tool>` argument and a target-aware pre-check.
- `docker/docker-entrypoint.sh` - section 3c, the two re-pointed writers plus the non-clobber guard on the api-key materialization, the stale comment at `:484`. Section 3c's shape depends on [Q4](#5-open-questions-for-zach).
- `bobi/tool_library/codex/tool.yaml` - honour `CODEX_HOME` (F12).
- `bobi/brain/codex_config.py:46-47` - the stale comment (F16).
- `moda-labs/moda-agents` - the false preflight claim, the on-demand trigger prompt, the runbook line.

**Out of scope.**

- Any second login path, Slack-specific or otherwise (Z3).
- A codex `requires:`/preflight gate, a `doctor` codex row, a command rename, new flags or exit codes.
- Automatic re-login on credential expiry. codex OAuth has no refresh expiry (`bobi/auth_bootstrap.py:166-171`), so this is once per machine.
- Any other CLI tool. `aichat`, `gh` and `venn` are env-var/API-key based and already durable through `run/.env` on the same volume; `gstack` has no auth.

**Accepted and stated, not guarded.**

- **A codex tool login is permitted alongside an ambient `OPENAI_API_KEY`**, because the shadow-env guard is scoped to the brain (3.3). The on-disk OAuth `auth.json` wins, since codex reads only that file (`docker/docker-entrypoint.sh:535-536`), and the non-clobber guard in 3.4 keeps it that way across boots.
- **A brain switch discards `${DATA_DIR}/codex/skills`** (3.4). Re-derived on the next boot of either brain; the only casualty is a skill installed into `~/.codex/skills` at runtime on a codex-brained machine, which nothing does today. Moot under Q4's file-level shape.
- `CODEX_HOME` moves codex's state (~101 MB today) to the volume. `/data` has 9.1 GB free of 15 GB. Moot under Q4's file-level shape, which leaves everything but `auth.json` on the overlay.
- In `api_key` mode a plaintext key lands on the volume, alongside `GH_TOKEN`, `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET` and `LINEAR_API_KEY`, which already live there at mode 600.
- codex writes PATH-alias helper binaries into `CODEX_HOME` on every invocation, so they land on the volume. `tests/integration/test_container_image.py:277` guards root-owned entries under `/home/bobi` but not under `/data/codex`. Section 3c's `chown` covers first boot; no root-run `codex` invocation exists in the entrypoint today. Moot under Q4's file-level shape.

## 7. Verification plan

Unit, `tests/test_auth_bootstrap.py` (baseline today: 78 passed in 2.51s).

1. **Ask-first ordering, `device_poll`.** The ask is posted and the reply is consumed **before** `spawn_login` is called. Assert on call order; this is the Z2 guarantee and the one test that would catch a regression to post-first.
2. **Ask-first ordering, `paste_back`.** Same, for the brain flow. Z4.
3. **A bot reply is not "ready".** An event carrying `fields.bot_id` does not satisfy the wait; a following human event does.
4. **A human thread reply with a *different* `thread_ts` is not "ready".** A `slack.thread_reply` in the login channel anchored on another thread is ignored, and one anchored on the ask's `ts` is accepted. Written against `fields.thread_ts`, which is the field the filter reads (3.2(d)). The "no `thread_ts` at all" case is deliberately not tested: the adapter never emits it (`chat-sdk-slack.ts:123-124`), so it is unreachable rather than untested.
5. **The device code is posted into the ask's thread**, not to the channel root. **Both post paths**, because they are different code and only the legacy one runs in production: legacy asserts `post_slack_message(..., thread_ts=<ask ts>)`, gateway asserts the destination ref is `<dest>:thread:<ask ts>`. One test over one path would ship the other unexercised.
6. **`target` retargets everything.** With a `claude` brain and `target="codex"`, the spawned command is `codex login --device-auth` and the credential path resolves under the codex spec's credential dir.
7. **`spawn_login` is still called with exactly one argument.** A fake with a `(home)`-only signature still binds when `target="codex"`. This is the assertion with content; "no injected fake breaks" would be a tautology while `:660` is byte-identical.
8. **Neither brain-credential guard refuses a tool target.** A Claude-gateway config with `ANTHROPIC_AUTH_TOKEN` set refuses `target=None` and permits `target="codex"`; the same for `OPENAI_API_KEY` set with `target="codex"`. The existing refusal test (`tests/test_auth_bootstrap.py:1220`) must still pass unchanged, because it drives `BRAIN_ENV` directly and so runs with `target is None`.
9. **`cli.py`'s pre-check is target-aware.** With Claude credentials present and `codex` requested, it proceeds instead of printing "already present".
10. **Unchanged:** `test_login_bootstrap_posts_only_to_the_configured_channel` (`tests/test_auth_bootstrap.py:1336`) must still pass. It pins #1009's non-caller-controlled destination.

Shell lane, runnable without docker (the 3.4 harness shape).

11. **The api-key materialization does not clobber a durable OAuth credential.** With an OAuth-shaped `auth.json` on the volume, `BOBI_AUTH=api_key` and `OPENAI_API_KEY` set, a boot leaves the file untouched and logs that it did. With an api-key-shaped file, it is replaced. This is the one new destructive path the change creates.
12. **Boot-order matrix.** claude, codex, claude on one persisted `${DATA_DIR}`, and the mirror. Every boot exits 0, and on each non-codex boot `$CODEX_HOME/skills` resolves and lists **both** a baked and a tool-installed skill. A same-brain two-boot test passes while the bug is present, so this is the leg that matters. Not needed under Q4's file-level shape, which creates no such object.

Docker lane, `tests/integration/test_container_image.py` (`-m docker`, needs a built image).

13. **Claude brain, subscription.** `/data/codex` exists and is bobi-owned, the credential path resolves to the volume in the manager's environment, and it survives a restart.
14. **Codex brain boots clean.** Boot succeeds, the brain credential is intact, and the baked skills are reachable (`readlink -f` on the skills path exits 0). This is the regression pin for the abort reproduced in 3.4.

Live, post-merge, owed on the issue as proof of work.

15. **One real login end to end.** `login-bootstrap codex`, a human reply in the thread, a real device code, authorize, then `codex exec -s read-only` returning 0 from a worker.
16. **Survives a restart.** Same credential after a machine restart, with no second login.

Items 1, 2, 3, 6, 9, 10, 13, 15 and 16 are unchanged from the round-1 spec.
Items 1 and 2 look redundant and are not: different `spec.flow` branches (`bobi/auth_bootstrap.py:662` vs `:683`).

## 8. Implementation plan

Not to be started until Gate 1 is approved, and not before Q1 and Q4 are answered - both change code.
Q2 and Q3 are prompt-text and policy only, and the spec states a recommendation for each.

1. Tests 1-5 (ask-first, human-only, thread-scoped, both post paths) against the current `run_bootstrap`, failing.
2. Change 1 (3.2), including the `_wait_for_chat_event` factoring. Tests 1-5 green, and the 78 existing tests stay green.
3. Tests 6-9, failing. Then change 2 (3.3). Test 7 is the pin for the superseded plan's F11.
4. Change 3 (3.4), in whichever shape Q4 selects: section 3c, the re-pointed sweep, the non-clobber guard, `tool.yaml` and the two stale comments if the directory shape is chosen. Then tests 11-14.
5. Review gate, full test run, PR.
6. The `moda-agents` companion PR (3.5).
7. Post-roll: tests 15-16 on the issue.


## Appendix A: verification record, 2026-10-09

Pinned to `origin/main` at `83bebe49`.
Round 1 read `origin/main` in a detached worktree at `/tmp/wt-958-main`; round 2 re-read it in a second detached worktree at `/tmp/wt-958-main-r2`, both cut with an explicit start-point at `83bebe49`.
Every round-2 citation below was re-read from that worktree rather than carried over.
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
| Guard + after-placement is correct for same-brain boots | both brains, two boots, exit 0, link resolves |
| Guard + after-placement ABORTS on a brain switch | claude then codex on one volume: `ln: failed to create symbolic link ...: File exists`, exit 1, link loops; still broken on boot 3 |
| **A skip-if-present guard silently loses the image's codex skills** | re-run in round 2 across 6 boot orders. `codex, claude`: boot 2 exits 0 with no log line and `$CODEX_HOME/skills` listing only the baked skill; the tool-installed `gstack-browse` is gone. Same on boot 3 of `claude, codex, claude` and boot 2 of `codex, claude, codex`. |
| **The shape in 3.4 is correct on every boot order** | same 6 sequences, same harness: every boot exits 0, `readlink -f` resolves, and every non-codex boot lists both `baked-a` and `gstack-browse` |
| Bare `[ -L x ] && rm -f x` is errexit-safe | executed standalone and mid-block under `set -euo pipefail` on bash 5.2.37 (the entrypoint's interpreter, `:1`): both reached the next line, `rc=0` |
| **codex writes `auth.json` in place, not by rename** | `codex login --with-api-key` over a symlink whose target exists: `Successfully logged in`, link intact, content in the durable target. Through a **dangling** symlink: durable target created at mode `600`, link intact. `codex login status` through a dangling symlink: `Not logged in`, no crash. This is what makes [Q4](#5-open-questions-for-zach) answerable. |
| `codex_auth_uses_api_key` classifies a codex-written api-key file | the file codex writes is `{"auth_mode":"apikey","OPENAI_API_KEY":...}`; the entrypoint predicate (`:336-355`) exits 0 on it, so the non-clobber guard still replaces a stale api-key file |
| Baseline suite | `pytest tests/test_auth_bootstrap.py -q` -> 78 passed in 2.51s |
| Live env | `BOBI_AUTH=subscription`, `BOBI_BRAIN=claude`, `BOBI_LOGIN_CHANNEL=#bobi-eng-team` (the legacy channel-name path), `CODEX_HOME` unset, `OPENAI_API_KEY` unset |

**Could not verify.**

- **An end-to-end login.** Needs a human to authorize a real device code. Each link is verified separately; the chain is not. This is verification 15.
- **The docker lane.** `-m docker`, needs a built image. No image build in this session.
- **That `/data/codex` behaves on the real volume.** Not created - that would be a live mutation during a spec step. The durability argument rests on `~/.claude -> /data/claude` on the same `/dev/vdc`.
- **Exact line counts.** Derived from the diffs this spec describes, not from an implementation.
- **A real thread reply arriving over the event bus.** The mechanism is verified by reading the code that already does it at boot and by the adapter fields it depends on; no live Slack round-trip was performed.
- **codex's OAuth device-flow write path specifically.** Q4's in-place-write evidence comes from `codex login --with-api-key`, the one write path reachable without authorizing a real device code. `--with-access-token` rejects a synthetic token before writing (`invalid agent identity JWT format`), so the `--device-auth` write was not observed. Both go through the same `auth.json` persistence, but that is read from behaviour, not from codex's source. Worth re-confirming on the first real login (verification 15) before relying on it.
- **Cross-model adversarial review.** Impossible here for the reason under review: `codex` 401s and `aichat` is unconfigured (`OPENROUTER_API_KEY`/`AICHAT_PLATFORM` unset). Single-model pass, stated as one.
