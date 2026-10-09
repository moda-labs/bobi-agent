# Ask-first `login-bootstrap`: one login primitive for the brain and for CLI tools

> **Status:** Draft, awaiting Gate 1 approval from Zach. No implementation until approved.
> **Tracking issue:** moda-labs/bobi-agent#958 · **Spec PR:** #959 (draft) · **Written:** 2026-10-09
>
> **Size:** ~95 lines across 8 files in `bobi-agent`, plus ~25 lines of prompt and doc text in `moda-agents`.
>
> Every `file:line` below was read from a grep run against `origin/main` at `70db2e1059fdf8f35751b17806496cb91f427b09`.
> [Appendix A](#appendix-a-verification-record) is the verification record, including what was executed live.
> Review reports are committed beside this file under [`plans/reviews/`](reviews/).

## 1. Problem

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
   It resolves its login spec from the process brain (`bobi/auth_bootstrap.py:117-120`, `_active_spec`), and the entrypoint invokes it bare (`docker/docker-entrypoint.sh:575`).
   On a Claude-brained team there is no way to ask it for `codex`.
2. **`codex` state is not durable.**
   `~/.codex` sits on the container overlay, not the volume.
   `CODEX_HOME` is honoured by the code (`bobi/auth_bootstrap.py:112`, `bobi/brain/codex_config.py:51-56`) but set by nothing: `grep -n 'CODEX_HOME' docker/docker-entrypoint.sh` returns no match.
3. **The device code can expire unread.**
   codex's one-time code lasts 15 minutes; `login-bootstrap --timeout` defaults to 600s (`bobi/cli.py:808-809`).
   Posting the code and hoping a human is watching is a race.
4. **The eng-team prompt claims a preflight that does not exist.**
   `moda-labs/moda-agents:agents/moda-eng-team/tools/codex.md:12` says codex is "preflighted by `agent.yaml` `requires:` (installed + authed)" and `:47` says "the preflight blocks dispatch".
   The composed `package/agent.yaml` has no `tool_library:` key, and the upstream source removed codex from it deliberately (`agents/moda-eng-team/agent.yaml:35-38`).
   So a worker is told the tool is guaranteed authed while it 401s.

## 2. The decisions this spec implements

Zach, Slack `#bobi-eng-team`, 2026-10-09. Quoted, not relitigated.

| # | Quote | What it settles |
|---|---|---|
| **Z1** | "we cannot assume the operator always has fly ssh access, so we need to use the human communication channel (in this case slack)." | The chat relay stays. "Cut the relay, use `fly ssh`" is rejected. |
| **Z2** | "Since it's a CLI tool, the need for auth could be discovered at any time." | The device code must not expire unread. Ask first; mint only after a human replies. |
| **Z3** | "We want to reuse the behavior for brain auth though so we dont have multiple ways of doing the same thing." | One primitive, not a brain path plus a tool path. |
| **Z4** | "I think the brain can also use ask-first. You can change the prompt to 'reply to this message in a thread when you are ready, and then I will begin the login flow.'" | Ask-first is unconditional, including the brain at boot. |
| **Z5** | "then you can judge any user message as 'ready'" | Any human message counts. No keyword, no allowlist. |

Zach ruled on eight design questions the same day. These are decided, not open.

| # | Ruling | Where it lands |
|---|---|---|
| **D1** | Boot waits on a long bounded timeout and **never re-posts**. One ask ever. On timeout, exit non-zero as today; the next boot silently re-attaches to the stored ask and does one catch-up read of its thread before blocking. Clear the stored id on success. **No supervisor changes.** | [4](#4-boot-behaviour-when-nobody-replies) |
| **D2** | The tool trigger is **director-only**, as prompt policy. | [3.6](#36-companion-change-in-moda-labsmoda-agents) |
| **D3** | One pending ask per tool is **prompt-only dedup. No lock.** | [5](#5-scope), accepted and stated |
| **D4** | `CODEX_HOME` moves codex's **whole config dir** to the volume, not just the credential file. | [3.4](#34-change-3-codex_home-on-the-durable-volume) |
| **D5** | **One correlation rule for all transports**: a channel destination requires the thread anchor, a DM accepts any human message. Discord guild channels are out of scope. | [3.2](#32-change-1-ask-first-in-run_bootstrap) |
| **D6** | **No new guard** for a gateway-brained team minting a direct provider credential. State it as a limitation. | [5](#5-scope), accepted and stated |
| **D7** | **Yes to a human-gated in-flight rebind in v1**, so a revoked or wrong-account credential heals without `fly ssh`. Quarantine happens there and only there. | [3.5](#35-change-4-human-gated-rebind) |
| **D8** | The boot sweep stays **conservative**: recognized API-key shape only. The `--device-auth` fixture is observed, so nothing blocks on it. | [3.4](#34-change-3-codex_home-on-the-durable-volume) |

Standing bar: simplest practical solution, no cruft.

## 3. Solution

One primitive, ask-first, used by both triggers.

`login-bootstrap [<tool>] [--rebind]`:

1. Resolve and validate the target. Resolve the login spec from it.
2. Short-circuit if the credential is already valid, unless `--rebind` was passed.
3. Resolve the login channel from `$BOBI_LOGIN_CHANNEL`. Destination stays configuration, never a caller's argument (#1009).
4. Register the event listener and wait for its socket to go live.
5. Re-attach or ask, exactly one of the two:
   - A stored ask for this login kind exists: do one catch-up read of its thread and post nothing.
   - No stored ask: post the ask, keep the posted message's id, persist it.
     *"Reply to this message in a thread when you are ready, and then I will begin the login flow."*
6. Block until a **human** replies.
7. Spawn the login CLI, scrape the sign-in URL (and, for `device_poll`, the one-time code), and post them **into that thread**.
8. Claude `paste_back`: wait in the same thread for the code, write it into the pty.
   Codex `device_poll`: wait for the CLI to exit as the human authorizes.
9. Reap the login process, post the outcome into the thread, verify the credential on disk, and clear the stored ask id if it landed.

Two triggers, one behaviour:

- **Brain at boot.** The entrypoint's existing bare call (`docker/docker-entrypoint.sh:575`), unchanged.
- **CLI tool on demand.** The director runs `login-bootstrap codex` when a worker reports a 401.

**The listener is registered before the ask is posted, and that order is load-bearing.**
This repo states the contract twice: "Replay is recovery, not a substitute for establishing the requested live subscription first" (`bobi/events/client.py:210-211`) and "Subscribe before publish, and only publish once the subscription's WS is actually live" (`bobi/inbox.py:444-447`).
A reply lost in the gap between post and subscribe is **unrecoverable**, not merely late: replay is cursor-based per deployment (`docs/EVENT_SERVER.md:434-441`), and each `register` call mints a fresh deployment id with a fresh session (`event-server/core/src/core.ts:1453`, `:1471`), so a deployment created after an event was published was never an indexed subscriber for it.
`_register_login_channel` and `register` are remote calls, so the window is not instantaneous.

### 3.1 The wait mechanism already exists, and already runs at boot

This was the one feasibility risk worth checking, because at boot the brain is dead and nothing is listening for a reply.
It is not a risk: `login-bootstrap` already does its own waiting today.

`_wait_for_code` (`bobi/auth_bootstrap.py:513-579`) subscribes to the chat topic over the event server and blocks on a queue:

- It reads the event server URL from config (`bobi/auth_bootstrap.py:522`; `event_server_url` at `bobi/config.py:485`), which is a **remote** HTTP endpoint, not an in-container process.
- It registers a listener (`bobi/auth_bootstrap.py:543-546`), streams events through `EventServerClient` (`:561-562`) and blocks on `wait_connected` (`:563`) before reading the queue.
- The Claude `paste_back` flow calls it at boot already (`bobi/auth_bootstrap.py:677`).

The boot ordering makes the brain's absence irrelevant: `login-bootstrap` runs in entrypoint section 4 (`docker/docker-entrypoint.sh:565-577`), and the manager is not `exec`'d until section 5 (`:590-640`).
The waiter is `login-bootstrap`'s own process.

**The on-demand trigger also works, because delivery is fan-out, not competing consumers.**
On demand the manager is already running and subscribed to the same chat topic, so a competing-consumer bus would let the manager swallow the reply and starve the `login-bootstrap` listener forever.
It does not: `admittedDeploymentIds` (`event-server/core/src/core.ts:1939`) accumulates every admitted subscriber into a `Set` and the runtime delivers to all of them (`docs/EVENT_SERVER.md:417`, and non-global keys "admit every indexed subscriber", `:414-415`).

One consequence, for the companion prompt change: **the director will also see the reply**, as an ordinary thread message.
It must not re-handle it or answer it; `login-bootstrap` owns that thread until it posts its outcome.
The delivery circuit breaker is not a risk, because it trips only on an agent's own output looping back "without a human event in between" (`docs/EVENT_SERVER.md:420`), and the reply is a human event.

**No new polling loop is needed, and no Slack-specific read path is added.**
`conversations.replies` appears nowhere in `bobi/` on main: `grep -rn 'conversations\.replies' .` matches exactly two lines, both in `tests/integration/test_channel_gateway.py`, a test stub's dispatch table and an assertion over it.
A direct Slack poll would be a second way to read a reply, which Z3 forbids, and it would work only on Slack.
The one catch-up read in step 5 is not that: it is a single bounded read of the ask's own thread through the gateway primitive that already exists (`channels_history`, `bobi/events/gateway.py:96-103`), on the same transport as the push path, and it happens once per process rather than in a loop.

### 3.2 Change 1 - ask-first in `run_bootstrap`

Five pieces, all in `bobi/auth_bootstrap.py`.

**(a) Return the posted message id.**
`_post_login_message` (`:382-390`) currently returns `None` and discards both post paths' results.
Both already carry the id: the legacy Slack path returns `chat.postMessage`'s parsed body (`bobi/slack.py:474-481`), and the gateway path's `channels_send` documents `ts` as "the posted/updated message id" (`bobi/events/gateway.py:69-70`).
Return it.

**(b) Post into a thread.**
Both paths already support it, so this is argument passing, not new transport.

- Legacy: `post_slack_message` already takes `thread_ts` (`bobi/slack.py:478`).
- Gateway: posting to a `<destination>:thread:<ts>` ref threads the message.
  The Slack channel adapter maps a parsed `threadId` straight onto `threadTs` (`event-server/core/src/channels.ts:338`), and `slack` declares `threads: true` (`:313`).
  The ref grammar is `<source>:<scope>:<chat_type>:<chat_id>[:thread:<thread_id>]` (`bobi/conversation.py:10`; `build_conversation` at `:32`).

**(c) One wait function, split into connect and wait.**
Factor `_wait_for_code`'s subscribe-and-JOIN body (`:513-563`) into `_connect_chat_listener(project_path, channel, timeout)` returning a live client plus its queue, and `_wait_for_chat_event(client, queue, timeout, match)` consuming it.
The post happens between the two calls, which is what makes the ordering above expressible rather than a comment.
Two match predicates then share one transport:

- `_is_ready_reply` - any human message correlated to the ask (see (d)), returning the address to post into.
  On the gateway path that is the inbound event's `conversation` ref, which is already thread-anchored (`:173` in the Slack adapter).
  On the legacy Slack path there is no ref to use, so it returns the pair the legacy poster takes: the configured `legacy_slack_channel` plus the ask's `thread_ts`.
- `_extract_code` - today's predicate (`:473-510`), now also correlated the same way.

**(d) One correlation rule for every transport (D5).**
The rule is a property of the **destination**, not of the source, and the predicate for it already exists in this file.
`_paste_back_instruction`'s `is_channel` (`:456-461`) is exactly "is this destination a shared channel", covering both a `:channel:` conversation ref and the legacy Slack channel-id path.
Reuse it:

| Destination | Ready when | Why |
|---|---|---|
| **Channel** (`is_channel` true) | `fields.thread_ts == <the ask's ts>` | The channel sees unrelated traffic, so the thread anchor is the only correlation. |
| **DM** (`is_channel` false) | any human message that passes the existing source and conversation filters | A 1:1 DM has nothing to correlate against. The conversation is the correlation. |

Human-only, on both branches: reject an event carrying `fields.bot_id`.
The Slack adapter sets `fields.bot_id` for any bot-authored message (`event-server/core/src/adapters/chat-sdk-slack.ts:154`, `:168`) and already drops messages from *our own* bots (`:99-100`), so this only adds third-party bots.
Per Z5 there is no keyword and no allowlist.

**This fixes a live bug and it is why the rule is one rule rather than three.**
`fields.thread_ts` is set only when the inbound message carries `thread_ts` (`adapters/chat-sdk-slack.ts:166`).
A plain reply in a **Slack DM** has none, so an anchor-always filter rejects it.
The same rule carries Discord DMs and WhatsApp, which have no thread anchor at all and declare `threads: false` (`event-server/core/src/channels.ts:644`, `:483`), with **zero** adapter changes.

**The anchor filter reads `fields.thread_ts` and must not also filter on the event type.**
`threadTs` is read once, before any type branching (`adapters/chat-sdk-slack.ts:106`), and `fields.thread_ts` is set for every emitted type (`:166`).
So `slack.thread_reply` is not the only type that can carry the ask's `ts`: a human who @mentions the bot *inside* the ask's thread is emitted as `slack.mention` with `fields.thread_ts` set, because `app_mention` takes the branch at `:115-116` before the dedup branch at `:117-118`.
Under Z5 that message is ready, so an event-type filter would reject a human reply the design must accept.

**What the channel branch excludes, stated correctly.**
Not loose channel chatter: the adapter already drops that.
A channel `message` with no self-mention and no `thread_ts` falls through to `return { event: null }` (`:123-124`), and a channel `message` that *does* mention the bot is dropped as a duplicate of the `app_mention` copy (`:117-118`).
The real exposure is narrower: an unrelated **thread reply** elsewhere in the login channel (`:121-122`, `slack.thread_reply`) or a **top-level @mention** of the bot in that channel (`:115-116`, `slack.mention`, no `thread_ts`).
Both carry `fields.channel` equal to the login channel and both satisfy today's filter.

**A Discord guild-channel destination is refused up front.**
Under the rule above it is a channel destination requiring an anchor, and the Discord adapter emits none: its `fields` are `user_id`, `user_name`, `channel_id`, `message_id`, `application_id` (`event-server/core/src/adapters/discord.ts:108-114`), and the Gateway's `referenced_message` is read only to classify `isReplyToBot` (`:54-56`).
So no inbound event could ever satisfy it and the run would hang to timeout.
`_resolve_login_channel` rejects a `discord:<scope>:channel:<id>` ref with an error naming the gap: supporting it needs the adapter to emit the referenced-message id, which is an event-server change and a separate deploy.
Discord **DMs** stay supported, by the DM branch, with no adapter change.

**(e) Ask-first extends the Discord inbound requirement to the codex flow.**
`_ensure_discord_paste_back_ready` (`:393-437`) requires an event server running the **local** Discord Gateway driver (`:432-437`) and is called only for `paste_back` today (`:656-657`).
Under ask-first a codex-target Discord login also needs inbound Discord events, so the same requirement now applies to `device_poll`.
Widen the call to both flows and rename it `_ensure_discord_inbound_ready`, because the old name no longer describes when it fires.

### 3.3 Change 2 - the `<tool>` target

`run_bootstrap` already seeds the process brain from config at `bobi/auth_bootstrap.py:626` (`set_process_brain_from_config(cfg)`), and every downstream helper resolves its spec from that env var.
Overriding it one line later retargets the whole flow.

```python
set_process_brain_from_config(cfg)
if resolved_kind:
    os.environ[BRAIN_ENV] = resolved_kind    # the tool's spec, not the brain's
spec = _active_spec()
```

**Verified, not assumed.** Executed against a worktree pinned to `origin/main` with `PYTHONPATH` pinned to it:

```
after set_process_brain_from_config(claude):  claude /data/claude/.credentials.json
after os.environ[BRAIN_ENV]='codex':          codex  /data/codex/auth.json
```

One assignment flips `spec.kind`, `login_cmd` (`claude auth login --claudeai` -> `codex login --device-auth`), `flow` (`paste_back` -> `device_poll`), `shadow_env` (`ANTHROPIC_API_KEY` -> `OPENAI_API_KEY`) and `credentials_path`.

**The call-site injection point is untouched.**
`spawn_login(home)` at `:660` is an injection point (`spawn_login = spawn_login or _spawn_login`, `:604`), and every fake that reaches it has a `(home)`-only signature, so threading a `spec` argument through it would raise `TypeError`.
Counted rather than estimated: `grep -n spawn_login tests/test_auth_bootstrap.py` returns 11 lines.
Six are `spawn_login=lambda h: None` inside `pytest.raises` guards (`:717`, `:818`, `:834`, `:849`, `:855`, `:1227`) that raise before `:660`.
Four are `def fake_spawn(home)` injections passed at `:621`, `:692`, `:811` and `:1209`, of which three reach `:660` and `:811`'s asserts it is never called.
One monkeypatches the module attribute (`:1289`).
The env override leaves `:660` byte-identical, so no fake breaks.
`_spawn_login` re-reads `_active_spec()` internally (`:213`), so it picks the target up for free.

**Validate the target against the login specs, not the brain registry.**
This is the difference between a closed input and a guard bypass.
`_active_spec()` falls back to Claude for any unrecognized value (`:117-120`, `_SPECS.get(kind, _SPECS["claude"])`), so an unvalidated `target` silently runs the **Claude** flow.
The brain registry is the wrong allow-list: `known_brain_kinds()` returns `sorted(_BRAINS)` (`bobi/brain/__init__.py:490-494`) and `_BRAINS` contains `stub` (`:57`), while `_SPECS` holds only `claude` and `codex` (`bobi/auth_bootstrap.py:92-114`).
It also misses two spellings the rest of the tree honours, because `BRAIN_KIND_ALIASES` (`gateway -> claude`, `gateway-openai -> codex`, `bobi/brain/__init__.py:64-67`) is not in `known_brain_kinds()`.

```python
resolved_kind = normalize_brain_kind(target) if target else None   # bobi/brain/__init__.py:82
if target and resolved_kind not in _SPECS:
    raise RuntimeError(f"login target '{target}' is not one of: claude, codex")
```

**Scope both brain-credential guards by the resolved provider, not by argument presence.**
`run_bootstrap` carries two refusals, and both are arguments about a *credential path*, not about whether an argument was supplied:

- The **gateway guard** (`:614-625`) raises for a gateway brain that is not Claude, or a Claude gateway with `ANTHROPIC_AUTH_TOKEN` set. Its own message says what it is about: "this gateway authenticates with gateway credentials in the runtime .env."
- The **shadow-env guard** (`:635-639`) raises when `os.environ[spec.shadow_env]` is set.

Both gate on the same predicate, `resolved_kind in (None, "claude")`, which is the Claude credential path and nothing else.
Exactly one case becomes newly permitted: `login-bootstrap codex` on a non-codex brain, which is the case #958 is about.
`login-bootstrap claude` keeps both guards, which is what the argument-presence form got wrong.

```python
claude_credential = resolved_kind in (None, "claude")
if cfg.brain_is_gateway and claude_credential and (...):   # :614-625, body unchanged
    raise RuntimeError(...)
...
if claude_credential and os.environ.get(spec.shadow_env):  # :635-639, in place
    raise RuntimeError(...)
```

The two guards stay where they are and must not be made adjacent: `set_process_brain_from_config(cfg)` (`:626`), `spec = _active_spec()` (`:627`) and the credential short-circuit (`:630-633`) sit between them, and the shadow-env guard reads `spec.shadow_env`.

The codex exemption is the one worth justifying.
`docker/docker-entrypoint.sh:535-536` states the premise outright: "Unlike Claude, Codex does not read OPENAI_API_KEY directly; it expects ~/.codex/auth.json."
So an ambient `OPENAI_API_KEY` does not shadow a codex OAuth credential on disk the way `ANTHROPIC_API_KEY` shadows Claude's; it only becomes codex auth if the entrypoint materializes it, which subscription mode refuses to do (`:549-550`).
Keeping the refusal would stop any claude-brained team that sets `OPENAI_API_KEY` for unrelated tooling from ever logging codex in, with no credential conflict to justify it.
`tests/test_auth_bootstrap.py:1221-1227` passes unchanged: it drives `BRAIN_ENV` directly rather than passing `target`, so `resolved_kind is None` there and the refusal is still pinned.

And two small edges:

- **Make `cli.py`'s pre-check target-aware.** `bobi/cli.py:827` calls `auth_bootstrap.credentials_exist()` before `run_bootstrap` (`:831`). On a Claude brain with a `codex` target it would check Claude's credential and print "already present - nothing to do" (`:828`). The CLI argument is added beside `--timeout` at `:808-809`.
- **`run_bootstrap`'s own short-circuit needs no change.** `:630-633` returns `True` when the credential already exists, and it reads `credentials_path(home)` *after* the `BRAIN_ENV` override, so it is target-aware for free. A second `login-bootstrap codex` after a successful first one is a clean no-op.

**Do not wire `_refuse_runtime_lifecycle` into this command.**
It refuses manager descendants (`bobi/cli.py:514-525`, `caller_is_manager_descendant` at `bobi/service.py:303`), and the director is one, so it would refuse the on-demand trigger outright.
It is wired only into `stop` and `restart` (`bobi/cli.py:1221`, `:1265`); leave it there.
Ask-first supplies the protection instead: an injected worker firing `login-bootstrap codex` posts only "reply when you are ready", and no credential-granting link exists until a human replies.

### 3.4 Change 3 - `CODEX_HOME` on the durable volume

Without this, a login that succeeds is lost on the next roll, and the 8 workers do not share it.

New section 3c in `docker/docker-entrypoint.sh`, placed **between `:532` and `:538`**.
The interval matters, not just the floor: the two follow-on edits below consume `${CODEX_HOME}` at `:547` and `:557`, and under `set -euo pipefail` (`:14`) a later placement is `CODEX_HOME: unbound variable` and an immediate boot abort on every machine.

```sh
# --- 3c. Codex's durable config dir, both brains (#958) ---------------------
# On a codex BRAIN this is already ~/.codex: section 3b points that at the same
# directory, so the export just names it. On any other brain it moves codex's
# config dir onto the volume so a human-minted auth.json survives a roll.
# mkdir -p is required, not tidiness: codex exits 1 with "CODEX_HOME points to
# ..., but that path does not exist". chown because the warm-boot chown list
# (:375-378) does not cover this path.
# The lstat refusal is NOT defensive clutter: this runs as root (:5-8) against a
# path the bobi user owns (:370-373), and chown/mkdir -p both follow a symlink.
if [ -L "${DATA_DIR}/codex" ] || { [ -e "${DATA_DIR}/codex" ] && [ ! -d "${DATA_DIR}/codex" ]; }; then
  log "FATAL: ${DATA_DIR}/codex is not a directory; refusing to touch it as root"
  exit 1
fi
mkdir -p "${DATA_DIR}/codex"
chown "${APP_USER}:${APP_USER}" "${DATA_DIR}/codex"
export CODEX_HOME="${DATA_DIR}/codex"
```

No branch on the brain, no `ln`, no `rm -rf`, and section 3b is untouched.
The export reaches the manager: `exec gosu ... env "HOME=..." ... bobi agent <name> supervise` (`:637`, `:639`) and `as_app` (`:416`, `:418`) both use `env` without `-i`, so an exported variable is inherited rather than dropped. Measured: `FOO=bar sh -c 'export FOO; env HOME=/x env'` still prints `FOO=bar`.

**Do not reintroduce a `${DATA_DIR}/codex/skills` link.**
An earlier shape created it, and needed that one persistent path to be a **different kind of object per brain**: a real directory owned by 3b on a codex brain, a symlink out to the image `HOME` on any other.
Two writers, two required shapes, one path, on a volume that outlives the brain choice.
That collision produced a defect in five separate reviews (an `ln` over 3b's real directory aborting the boot, a symlink loop, silently lost image skills, a cleanup line unreachable on an image with no baked skills, and a link to itself).
The settling case: `${HOME}/.codex` is written in exactly one place, `:486-531`, inside the codex-brain block, and nothing on a non-codex boot restores it to a real directory, so a rootfs that has booted codex once keeps `${HOME}/.codex -> ${DATA_DIR}/codex` forever (the entrypoint states the distinction itself at `:400-401`).
The brain can change without a new rootfs, because it is read from the installed team on the volume when `BOBI_BRAIN` is unset (`:276-278`).

**Nothing reads `${CODEX_HOME}/skills`, which is what makes dropping it safe.**
`grep -rn 'codex_home()' --include=*.py .` returns six sites (`bobi/brain/codex.py:387`, `bobi/brain/codex_config.py:51`, `:181`, `:208`, `bobi/chat_history.py:317`, `bobi/brain/instructions.py:123`) and none touches `skills`.
The in-repo references to `~/.codex/skills` are gstack's installer (`bobi/tool_library/gstack/tool.yaml:33`, `:35`, `:51`) and its guide (`bobi/tool_library/gstack/guide.md:7-8`), all by literal `$HOME` path, which the export does not move.

**Four follow-on edits, each because this export falsifies something.**

1. **The api-key materialization must not clobber a human-minted credential** (`:547`).
   `materialize_codex_api_key_auth` writes unconditionally, and under the export its target on a non-codex brain is the durable file.
   So in `api_key` mode, skip the write when a usable OAuth credential is already there.
   Reuse `python -m bobi.auth_bootstrap credential-status codex <path>`, which the script already calls at `:569-571`; **no new shell predicate**.
   Add the same lstat refusal in front of the write itself: it runs as root (`:324-331`) and `path.write_text` and `path.chmod` both dereference, and its cleanup `chown -R "${APP_USER}:${APP_USER}" "${cred_dir}"` (`:333`) dereferences its top-level argument.
   `chown -h` is **not** an acceptable substitute: it retitles the link inode and the following root write still lands on the target.
2. **Re-point the subscription sweep** (`:557`), from `${HOME}/.codex` to `${CODEX_HOME}`, which is where the file now lives.
   The predicate itself is **unchanged** (D8): `codex_auth_uses_api_key` (`:336-355`) deletes only the recognized API-key shape, exits 1 on anything it cannot classify, and leaves an unclassifiable file alone.
   The reason to keep the sweep at all, which the issue asked and no earlier draft answered: it is not what unblocks OAuth, because `codex login --device-auth` deletes `auth.json` itself the moment it starts (measured).
   It is what keeps the machine off a stale API-key credential during the ask-first wait, which under D1 is hours rather than the seconds it is today.
3. **`bobi/tool_library/codex/tool.yaml` - a retarget and the same guard.**
   Its `success:` (`:6`) and `fix:` (`:8`) reference codex's home by literal path, and `grep -n 'CODEX_HOME' bobi/tool_library/codex/tool.yaml` returns nothing, so under the export the check reads the wrong directory and fails closed.
   The retarget alone re-opens the clobber edit 1 just closed: `success:`'s `elif` branch writes an API-key `auth.json` unconditionally every time the preflight runs.
   So this is two changes, not one: honour `CODEX_HOME`, and skip the write when a usable OAuth credential is present.
   `tool.yaml:6` already carries its own copy of the credential-shape test in its subscription branch; fold that into the same `credential-status` call rather than keeping a second definition.
   Latent for eng-team, which declares no `tool_library:`; fail-closed and then destructive for the next team that does.
4. **Three stale comments and one stale claim.**
   `docker/docker-entrypoint.sh:484` ("codex has no config-dir override") stopped being true when `credentials_dir_env="CODEX_HOME"` landed.
   `bobi/brain/codex_config.py:46-47` ("The entrypoint symlinks ~/.codex at the durable volume") is true only on a codex brain; this change makes it unconditionally true for the config dir.
   `bobi/tool_library/codex/guide.md:42` claims the runtime "materializes `~/.codex/auth.json` from `OPENAI_API_KEY`", which the export falsifies.
   `bobi/tool_library/gstack/guide.md:7-8` claims skills are linked under `~/.codex/skills/`, which is false on a non-codex brain once 3c creates no such link.

**The export is a process-wide switch, so here is everyone who flips with it.**
`codex_home()` (`bobi/brain/codex_config.py:51-56`) has five callers beyond `credentials_path()`:

| Caller | Brain-gated? | Effect on a claude brain |
|---|---|---|
| `bobi/brain/codex_config.py:181`, `:208` | n/a, internal | Config writers; only reached via `codex.py:387`. |
| `bobi/brain/codex.py:387` | Yes, by construction | Inside `CodexBrain.make_session`. No change on a claude brain. |
| `bobi/brain/instructions.py:123` | Yes, `engine == "codex"` (`:120`) | No change on a claude brain. |
| `bobi/chat_history.py:317` | No, but codex-specific by construction | Reads `${CODEX_HOME}/sessions`, which is where codex now writes them. Correct, and durable rather than lost on a roll. |
| `bobi/brain/instructions.py:129-136`, `_all_brain_targets()` | **No** | Unions `instruction_targets(kind)` over `known_brain_kinds()`, so the cross-kind cleanup set now includes `/data/codex/AGENTS.md` instead of `~/.codex/AGENTS.md`. Benign: the set exists to strip managed blocks, and the file it strips them from is now the one codex reads. |

One further reader does not go through `codex_home()`, and a grep for it misses them:

| Caller | Brain-gated? | Effect |
|---|---|---|
| `bobi/brain_availability.py:50`, `_account_boundary("openai")` | No, but provider-gated | Hashes `$CODEX_HOME` or `~/.codex` into an account-boundary digest for incident tracking. The export changes that digest once, orphaning any existing openai-provider incident history. Benign and one-time; the openai branch is only reached for an openai-provider turn. |

That is the complete set.
`grep -rn 'CODEX_HOME' --include=*.py .` returns, outside tests, only these consumers plus `bobi/auth_bootstrap.py:112` and three docstrings (`bobi/chat_history.py:11`, `:308`, `bobi/brain/instructions.py:13`).

### 3.5 Change 4 - human-gated rebind

D7. Without it, a machine holding a **revoked, server-invalidated, or wrong-account** codex credential has no recovery that does not need `fly ssh`, which Z1 says cannot be assumed.

The gap is specific and it is not about expiry.
Codex validation requires only a non-blank `tokens.refresh_token` (`bobi/auth_bootstrap.py:162-171`); it never proves the token is live or bound to the intended account.
A structurally valid file short-circuits both entry points: `run_bootstrap` returns `True` at `:630-633`, and the CLI returns earlier still with "Subscription credentials already present" (`bobi/cli.py:827-828`).
So the on-demand recovery would do nothing in exactly the cases that produce a real 401.

**The smallest surface that closes it is one flag.**

`login-bootstrap <tool> --rebind`:

- Skips the credential short-circuit in both places, so the ask is posted even though a credential is present.
- Posts the same ask and waits for the same human reply. It adds **no** new trust surface: minting and rebinding are gated on the identical signal.
- On the reply, and only then, **quarantines** the existing file to a collision-free `auth.json.quarantined-<utc-timestamp>` that never overwrites an existing name, then runs the login as normal.
- Refuses if `auth.json` is not a regular file. The reason is correctness, not privilege: this runs as the `bobi` user, and renaming a symlink would move the link while leaving the real credential live, so the quarantine would silently not happen.
- Names the quarantined path in the outcome post, so a failed rebind is recoverable by the human who asked for it.

Quarantine lives here and nowhere else (D8). The boot sweep does not quarantine anything.

### 3.6 Companion change in `moda-labs/moda-agents`

Prompt and doc text only. No framework code. Not a blocker for the bobi-agent PR.

**(a) Delete the false preflight claim.**
`agents/moda-eng-team/tools/codex.md:12` and `:47`.
`agents/baohua/tools/codex.md:15` already carries the corrected shape ("**It is NOT preflighted on this team, and it may not be authenticated.**") and is the precedent to copy.

**(b) The on-demand trigger, as eng-team prompt text.**

- A worker that hits a codex 401 reports "codex needs login" and **proceeds**, disclosing the gap in its output. It does not block, retry, or fail silently.
- The **director**, and only the director, runs the login in the background (D2): `login-bootstrap codex` when no credential exists, `login-bootstrap codex --rebind` when one exists and a worker still 401s.
- One pending ask per tool, deduped by the director (D3). While an ask is open, workers keep disclosing the gap and no second ask is posted.
- The director will receive the human's ready reply itself, because delivery is fan-out (3.1). It must not answer it or re-handle the thread; `login-bootstrap` owns that thread until it posts the outcome.

**(c) Runbook line** in `moda-labs/moda-agents:bobi-deploy/docs/CONTAINERIZED_DEPLOYMENT.md` (the standalone `moda-labs/bobi-deploy` repo is archived; the live copy lives under `moda-agents`): `codex` is logged in once per machine via `login-bootstrap codex`, healed via `--rebind`, and the `fly ssh console` plus `codex login --device-auth` path remains the fallback for an operator who has a shell.

## 4. Boot behaviour when nobody replies

D1. **One ask ever. The ask is never re-posted.**

Boot blocks on the ready reply for `ask_timeout`, then exits non-zero exactly as today, so the machine restarts and `fly logs` keeps showing life.
The next boot does **not** post a second ask. It reads the stored ask id and silently re-attaches to the same thread.

**State: one file per login kind.**
`${DATA_DIR}/codex/.login-ask-<kind>`, holding the ask's destination and its message id.

- `<kind>` is the **resolved** login spec kind, never the raw argument. A codex-brained boot passes no target while the tool path passes `codex`, and those are one credential, so they must be one slot. On a claude brain the brain's ask and the codex tool's ask are two credentials and correctly two slots.
- Written with `atomic_write_text` (`bobi/fsutil.py:100`), which `CLAUDE.md` requires of any file bobi must still read after an abrupt death.
  Every access is a whole-document read **or** a whole-document write, never a load-mutate-save, so the companion `file_lock` that `CLAUDE.md` requires for read-modify-write state does not apply here and is not taken.
  That is a separate question from D3: a lock here would protect one file's bytes, not dedup two asks, and D3 forbids the latter.
- Named by `${DATA_DIR}/codex` rather than by `${CODEX_HOME}` so one directory, and therefore one lstat guard (3.4), covers both the credential and the ask state.
- **Invalidated, not trusted.** The stored destination must equal the currently resolved one, or the entry is treated as absent, otherwise an edited `$BOBI_LOGIN_CHANNEL` re-attaches to a thread in a channel the config no longer names. A post into a stored thread that fails is likewise treated as absent.
- **Cleared on success**, so a login months later does not reply into a dead thread.

**One catch-up read, so a reply in the restart gap is not lost.**
This is the one real defect that "do not re-post" introduces and that re-posting did not have: a human who answers between the timeout exit and the next listener's connect gets no response and no second ask.
Replay cannot cover it, because each `register` mints a fresh deployment with an empty buffer (`event-server/core/src/core.ts:1453`, `:1471`).
So on re-attach, after the listener is live and before blocking, read the ask's thread once through `channels_history` (`bobi/events/gateway.py:96-103`).

The read is one call and the predicate is derivable from its result alone, with no extra state.
`/channels/history` returns `{user, text, ts}` per message, oldest-first, for the thread the ref anchors (`event-server/core/src/core.ts:2417-2419`, `event-server/core/src/channels.ts:379-409`, `ConversationMessage` at `:119-124`).
A reply is fresh when its `ts` is greater than the newest message in the thread whose `user` **is** the bot's own user id, which `require_app_identity` already returns (`bobi/slack.py:302-303`).
A message with an empty `user` is not treated as human, so the rule fails closed.
That matters: without it, a boot that already consumed the reply and then timed out mid-login would re-consume it on every restart and re-post a device code each time, which is the Slack noise D1 exists to prevent.

The ref for that read is assembled on both post paths, so this is one code path.
On the gateway path the destination is already a conversation ref.
On the legacy Slack channel-id path the team id is available from the same `require_app_identity` call that `_slack_topic` makes (`bobi/auth_bootstrap.py:278-283`), giving `slack:<team>:channel:<C...>:thread:<ts>`.
The channel credential the read needs is registered by `_register_login_channel` (`:316-379`, `register_slack_workspaces` at `:348`), which runs on both paths before the listener JOINs.

**Five phase budgets, not one shared timeout.**
Today a single 600s budget spans the ask wait, the URL scrape and the device authorization, so a human who authorizes at minute eleven loses even though the device code is valid for fifteen.
The flow has five waits, and the Claude path has a second human wait the codex path does not (`flow="paste_back"` at `:98` versus `flow="device_poll"` at `:108`):

| Phase | Applies to | Budget | Source |
|---|---|---|---|
| 1. Ready-reply wait (the ask) | both | `ask_timeout`, 1800s | new |
| 2. URL and device-code scrape | both | `url_timeout`, 120s | exists (`:588`), unchanged |
| 3a. Pasted-code reply wait | `paste_back` | `timeout`, 600s | exists; this is what `--timeout`'s help already describes (`bobi/cli.py:808-809`) |
| 3b. Device authorization | `device_poll` | `device_timeout`, 900s, the device code's own lifetime | new |
| 4. Post-code process exit | both | 60s, as the paste_back path already does (`:680`) | exists on one path, extended to the other |

`--timeout` keeps its current documented meaning, phase 3a, so the flag's help text stays true.
It must not be silently reused as the phase-1 budget.
`ask_timeout` is a keyword argument with no CLI flag, because nothing needs to set it per invocation.

**Why 1800s and not hours.** The catch-up read makes a restart lossless, so a long wait buys nothing and a short cycle costs nothing in Slack noise.
What the value trades is observability: the manager is not started while boot waits (section 4 of the entrypoint precedes section 5, `:565-577` before `:590-640`), so a credential-less machine has no health surface for the length of the wait.
1800s bounds that dark window to half an hour and lets the restart show up in `fly logs` roughly twice an hour, while still being three times today's budget.
This is the one number in the spec a reader might reasonably set differently; the shape is D1's and the trade is stated.

**On timeout, reap the login process and say so in the thread.**
Today the device path logs a warning and falls through (`:702-705`), and the `finally` calls `proc.terminate()` with no `wait`, no `kill` escalation and no process-group signal (`:706-712`), against a child spawned `start_new_session=True` (`:223`) and so its own session leader.
A login CLI that ignores SIGTERM survives as an orphan holding the pty.
Signal the process group, `wait` with a bound, escalate to `kill`, and reap.
Post an explicit expired-or-cancelled outcome into the thread saying a fresh reply is required, so the thread does not end on "waiting for you to authorize" and a late reply has somewhere to read what happened.

**Register the listener under a unique deployment name.**
Today it is the fixed literal `"login-bootstrap"` (`:544`, and again on the `BubbleRejected` retry at `:556`), and registering an existing name in the same bubble deliberately removes the prior deployment's subscriptions and the deployment itself (`event-server/core/src/core.ts:1441-1451`).
So two runs in flight de-index each other: a human reply to the **first** ask reaches only the second listener, whose predicate rejects it, and the first command blocks to timeout having posted a live ask nobody can satisfy.
`login-bootstrap-<pid>` fixes it in one line and is required independently of D3, because D3's dedup is a policy and this is what makes a policy violation merely noisy rather than wedged.
The cost is that a crashed run's deployment is no longer superseded by name; it lingers in the subscription index receiving events nobody reads until the server evicts it, roughly 60s after its last socket (`docs/EVENT_SERVER.md:62-63`).

**Three failure modes abort before the timeout, and the ask-first design does not change them.**
Naming them because D1's wait is about silence, and these are not silence.

| Failure | Where it aborts | Behaviour |
|---|---|---|
| `BOBI_LOGIN_CHANNEL` unset or misconfigured | `bobi/auth_bootstrap.py:649-654`, before registration and before any post | Tight restart loop, no ask, no health surface. No ask id is stored, so the re-attach path adds nothing and loses nothing. |
| Event server unconfigured or unreachable | in the waiter at `:521-526`; `register` makes remote calls before any wait | Same. Because the listener is now registered before the post, this raises with no ask in existence, so there is no thread to be noisy in. |
| Chat post fails (channel outage) | after registration, at the post | No ask lands; boot aborts and retries on the next cycle. A failed post into a stored id is treated as absent, which covers the degenerate case. |

All three are crash loops with no ask. Handling them is a separate change (a distinguishable exit for "misconfigured" versus "waiting for a human", plus a backoff so a permanently misconfigured machine does not spin), and it is not in this spec's scope.

**The ask text names the agent, the instance and the target**, so two machines in one fleet do not post indistinguishable asks.
It also names a thread only when the destination is a channel, reusing the same `is_channel` predicate as the correlation rule (3.2(d)), because "reply in a thread" is wrong on a threadless DM.

## 5. Scope

**In scope.**

- `bobi/auth_bootstrap.py` - ask-first for both flows, listener-before-post ordering, thread-anchored posting, the one correlation rule, the unique deployment name, the `target` parameter with validation, both brain-credential guards scoped by resolved provider, the five phase budgets, process-group reaping, the re-attach plus catch-up read, and `--rebind` with its quarantine.
- `bobi/cli.py` - the optional `<tool>` argument, `--rebind`, and a target-aware pre-check.
- `docker/docker-entrypoint.sh` - section 3c with its lstat refusal, the re-pointed sweep, the non-clobber guard and lstat refusal on the api-key materialization, the stale comment at `:484`.
- `bobi/tool_library/codex/tool.yaml` - honour `CODEX_HOME`, and skip the `elif` write when a usable OAuth credential is present.
- `bobi/tool_library/codex/guide.md:42`, `bobi/tool_library/gstack/guide.md:7-8`, `bobi/brain/codex_config.py:46-47` - stale claims this change falsifies.
- `moda-labs/moda-agents` - the false preflight claim, the on-demand trigger prompt, the runbook line.

**Out of scope.**

- Any second login path, Slack-specific or otherwise (Z3).
- **Discord guild channels as a login destination.** They need the adapter to emit a referenced-message id, which is an event-server change and a separate deploy (3.2(d)). Discord and WhatsApp **DMs** are supported, with no adapter change.
- A codex `requires:`/preflight gate, a `doctor` codex row, a command rename, new exit codes.
- Supervisor changes of any kind (D1).
- Automatic re-login without a human. `--rebind` is human-gated by design.
- Any other CLI tool. `aichat`, `gh` and `venn` are env-var based and already durable through `run/.env` on the same volume; `gstack` has no auth.

**Accepted and stated, not guarded.**

- **A gateway-brained team's `login-bootstrap <tool>` mints a direct provider credential** (D6), outside the gateway that team authenticates through. Zero such teams exist: all 7 teams on `moda-labs/moda-agents` `origin/main` declare `kind` only and no `base_url`, this box's installed team is `brain: {kind: claude}` (`run/package/agent.yaml:114-115`), and `grep -rn base_url --include='*.yaml'` over `bobi-agent` main returns nothing. The right control the day one exists is a per-team policy knob, not the gateway guard at `:614-625`, which is about the brain's credential path and says so in its own message.
- **A codex tool login is permitted alongside an ambient `OPENAI_API_KEY`** (3.3). The on-disk OAuth file wins, because codex reads only that file (`docker/docker-entrypoint.sh:535-536`), and the non-clobber guard keeps that true across boots on a non-codex brain. On a codex-brained `api_key` team the pre-existing `:544` arm still rewrites its one credential every boot; that arm is out of scope here.
- **Concurrent runs are prevented by prompt policy only** (D3). If the policy is violated, two asks post and two login CLIs race to write one file. The unique deployment name means both listeners still receive their own replies, and codex writes `auth.json` by replacement, so the outcome is one valid credential rather than a corrupt one. The residual cost is duplicate Slack messages. The cheap local fix if that ever matters is a pidfile in the agent's run dir, not a new mode on `file_lock`, which takes `fcntl.LOCK_EX` unconditionally (`bobi/fsutil.py:180`).
- **An unclassifiable `${CODEX_HOME}/auth.json` survives every boot untouched and silently.** That is main's behaviour today (`:336-355` exits 1 on anything it cannot parse) and D8 keeps it. No boot diagnostic is emitted for a non-brain tool credential, because section 4's `credential-status` call checks the **brain's** path (`:567-571`); the reason is reported by `login-bootstrap codex` when the director fires it.
- **The listener shares the manager's `cursor.json`.** `EventServerClient` is constructed with no `cursor_path` (`:561`), which the client's own comment warns against across deployments. Benign and pre-existing: only `bobi/events/drain.py` ever acks, so nothing is written back, and a fresh deployment's replay buffer is empty regardless.
- **On a claude-brained machine that has never booted codex, `codex exec` does not see gstack's codex-side skill links** (3.4). Nothing in this repo reads `${CODEX_HOME}/skills`, codex creates its own `skills/.system` there on first invocation, and the baked team skills never reached `~/.codex/skills` in the first place.
- **`CODEX_HOME` moves codex's state to the volume.** Measured on a fresh `CODEX_HOME` after one `codex exec`: ~105 MB, of which ~103 MB is a plugins git clone under `.tmp/`, plus four sqlite databases with WALs, `sessions/`, `shell_snapshots/` and `skills/.system`. Live `~/.codex` is 107 MB and `/data` has 8.9 GB free of 15 GB.
- **In `api_key` mode a plaintext key lands on the volume**, alongside `GH_TOKEN`, `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET` and `LINEAR_API_KEY`, which already live there at mode 600.
- **codex writes PATH-alias helper binaries on every invocation**, so that content now lands on the volume: `${CODEX_HOME}/tmp/arg0/codex-arg0<rand>/` holding `apply_patch`, `applypatch`, `codex-execve-wrapper` and `codex-linux-sandbox`, with the previous directory removed so there is no unbounded leak. This repo already documents the behaviour and the incident it caused (`tests/integration/test_container_image.py:277-284`, "the gstack `--host auto` bake died on root-owned /home/bobi/.codex exactly this way at the 0.47.0 fleet roll"). Section 3c's `chown` covers the directory, 3c creates no sub-object, and `grep -n '\bcodex\b' docker/docker-entrypoint.sh` confirms no root-run `codex` invocation exists there.
- **While boot waits for the ready reply the machine has no manager and no health surface**, for up to `ask_timeout`. D1 accepts this and forbids the supervisor change that would remove it; the bounded wait and the lossless re-attach are what keep it visible and recoverable.

## 6. Verification plan

Unit, `tests/test_auth_bootstrap.py` (baseline today: 78 passed in 2.35s).

1. **Ordering, both flows.** Assert the sequence `listener connected -> ask posted -> ready reply consumed -> spawn_login`, for `device_poll` and for `paste_back`. These are different `spec.flow` branches (`:662` versus `:683`), and this is the Z2/Z4 guarantee plus the subscribe-before-post contract in one assertion.
2. **A bot reply is not ready.** An event carrying `fields.bot_id` does not satisfy the wait; a following human event does.
3. **Correlation, channel destination.** On `fields.thread_ts` alone and never on the event type:

   | Event | `fields.thread_ts` | Ready? |
   |---|---|---|
   | `slack.thread_reply` in the ask's thread | the ask's `ts` | **yes** |
   | `slack.thread_reply` in another thread | another `ts` | no |
   | `slack.mention`, top-level in the login channel | **absent** | no |
   | `slack.mention`, **inside the ask's thread** | the ask's `ts` | **yes (Z5)** |

   The fourth leg is the one an implementer gets wrong: finding that a `thread_ts`-only predicate accepts a `slack.mention` and "fixing" it with an event-type filter rejects a human message in the ask's own thread.
4. **Correlation, DM destination.** A Slack DM reply with **no** `thread_ts` is ready. This is the bug an anchor-always filter ships. Same for a Discord DM and a WhatsApp DM, which carry no anchor at all.
5. **A Discord guild-channel destination is refused** at `_resolve_login_channel`, with an error naming the event-server change required. A Discord DM ref still resolves.
6. **The Discord inbound requirement now covers `device_poll`.** A codex-target Discord login against an event server with no local Gateway driver raises, where today only `paste_back` does.
7. **The URL and device code are posted into the ask's thread**, on **both** post paths, because they are different code and only the legacy one runs in production: legacy asserts `post_slack_message(..., thread_ts=<ask ts>)`, gateway asserts the destination ref is `<dest>:thread:<ask ts>`.
8. **The outcome post lands in the thread too**, for success, for failure, and for timeout.
9. **`target` retargets everything.** With a `claude` brain and `target="codex"`, the spawned command is `codex login --device-auth` and the credential path resolves under the codex spec's credential dir.
10. **`spawn_login` is still called with exactly one argument.** A fake with a `(home)`-only signature still binds when `target="codex"`.
11. **Target validation, by named case.** `stub` is rejected (it is a known *brain* kind, so a generic typo case passes while this one does not), `gateway-openai` resolves to codex, an unknown value is rejected with the accepted values named, and `claude` is accepted.
12. **Guard scoping by resolved provider.** A Claude-gateway config with `ANTHROPIC_AUTH_TOKEN` set refuses `target=None` **and** `target="claude"`, and permits `target="codex"`; `OPENAI_API_KEY` set refuses `target="claude"` and permits `target="codex"`. `tests/test_auth_bootstrap.py:1221-1227` passes unchanged.
13. **D6's limitation is asserted as documented behaviour**, not as a guard: on a gateway-brained config, `login-bootstrap codex` is permitted and resolves the codex spec. The test's docstring states that this mints a direct provider credential and names the per-team policy knob as the future control, so the limitation cannot be deleted silently.
14. **`cli.py`'s pre-check is target-aware.** With Claude credentials present and `codex` requested, it proceeds instead of printing "already present".
15. **Re-attach posts nothing and sees a gap reply.** With a stored ask id and a thread whose newest message is a human one, no ask is posted and the login proceeds. With a thread whose newest message is the bot's own, the catch-up read yields nothing and the run blocks. A message with an empty `user` is not treated as human.
16. **Stored-ask invalidation.** A stored destination that no longer matches the resolved one is treated as absent. A failed post into a stored id is treated as absent. The id is cleared on success and only on success.
17. **Phase budgets do not share.** A phase-1 wait that consumes its whole budget does not reduce phase 3b's, and a ready reply arriving one tick after the phase-1 deadline does not rescue the run: it is dropped, and the thread carries the expired outcome post.
18. **Reaping.** A fake login process that ignores `terminate` is signalled by process group, waited with a bound, escalated to `kill`, and reaped.
19. **Rebind.** With a structurally valid credential present: bare `login-bootstrap codex` short-circuits, `--rebind` posts the ask, and the human reply quarantines the credential to a collision-free name before the fresh login runs. A second rebind in the same second does not overwrite the first quarantine file. An `auth.json` that is a symlink is refused rather than renamed.
20. **Two concurrent runs do not de-index each other.** Two unique-named listeners each receive the reply to their own ask. This is the regression pin for the fixed-name supersede.
21. **Unchanged:** `test_login_bootstrap_posts_only_to_the_configured_channel` (`tests/test_auth_bootstrap.py:1336`) must still pass. It pins #1009's non-caller-controlled destination.

Shell lane, runnable without docker.

22. **Boot-order matrix, with real assertions.** claude, codex, claude on one persisted `${DATA_DIR}`, and the mirror, with `/opt/bobi/skills` present and absent, and with `${HOME}` persisted across boots as well as recreated. The harness **must** persist `${HOME}`: a per-boot-fresh `${HOME}` is a `fly deploy`, not a same-machine restart, and that axis is what hid an earlier blocker. Each boot asserts exit 0 **and** that `CODEX_HOME` is exported as `${DATA_DIR}/codex`, that the path is a real directory owned by `${APP_USER}`, and that both hold across a brain switch. Exit 0 alone passes with section 3c omitted entirely, which is not a regression pin.
23. **Planted symlinks, one outcome.** `${DATA_DIR}/codex -> /etc` and `${CODEX_HOME}/auth.json -> /etc/passwd`, separately. Boot exits non-zero with the diagnostic, **and** the protected target's ownership, mode and content are untouched. Fail, not repair: neither state is one any boot path creates, so repairing it silently discards the only signal that something planted it. The `[ -L "${HOME}/.codex" ] && rm -f` line at `:332` is not this case and is unaffected, because that path is one the entrypoint itself creates.
24. **The credential guard and the conservative sweep, five states x both auth modes.** One table, because the two arms are the same decision read in opposite directions.

    | `${CODEX_HOME}/auth.json` | `api_key` + `OPENAI_API_KEY` set | `subscription` |
    |---|---|---|
    | absent | materialized | no-op, **and no log line** |
    | api-key shaped | replaced | removed |
    | truncated or unparseable | replaced | **left intact**, no log line |
    | `{"tokens":{"refresh_token":""}}` | replaced | **left intact**, no log line |
    | OAuth, refresh token present | **left intact**, with a log line | **left intact** |

    The absent row's "no log line" and the two left-intact rows are the pins for keeping the predicate conservative: a widened "delete anything `credential-status` rejects" would log on every pre-login boot of every subscription machine and would delete an unobserved schema.
25. **The export reaches the manager.** An exported `CODEX_HOME` survives `gosu "${APP_USER}" env "HOME=..." ... bobi ...` (`:416`, `:637`), because `env` without `-i` inherits. Pinned because an `env`-allowlist reading of those lines would wrongly conclude the variable needs adding to four call sites.

Docker lane, `tests/integration/test_container_image.py` (`-m docker`, needs a built image).

26. **Claude brain, subscription.** `/data/codex` exists and is bobi-owned, `CODEX_HOME` resolves to it in the manager's environment, the credential path resolves to `/data/codex/auth.json`, and all of it survives a restart.
27. **Codex brain boots clean.** Boot succeeds and the brain credential is intact. This is the regression pin for the abort an earlier shape of 3c reproduced.

Live, post-merge, owed on the issue as proof of work.

28. **One real login end to end.** `login-bootstrap codex`, a human reply in the thread, a real device code, authorize, then `codex exec -s read-only` returning 0 from a worker.
29. **Survives a restart, and re-attaches silently.** The same credential after a machine restart with no second login. Separately: a restart **mid-wait** posts no second ask, and a reply sent during the gap is picked up by the catch-up read.

## 7. Implementation plan

Not to be started until Gate 1 is approved.

1. Tests 1-8 (ordering, human-only, the one correlation rule, both post paths, the outcome post, the Discord refusals) against the current `run_bootstrap`, failing.
2. Change 1 (3.2), including the connect/wait split. Tests 1-8 green, and the 78 existing tests stay green.
3. Tests 9-14, failing. Then change 2 (3.3). Test 10 is the pin that the `spawn_login` call site stayed `(home)`-only.
4. Tests 15-18 and 20-21, failing. Then the re-attach, the catch-up read, the phase budgets, the reaping and the unique deployment name (4).
5. Test 19, failing. Then `--rebind` (3.5).
6. Change 3 (3.4): section 3c, the re-pointed sweep, the non-clobber guard and the lstat refusals, the `tool.yaml` retarget plus its own guard, and the four stale claims. Then tests 22-27.
7. Review gate, full test run, PR.
8. The `moda-agents` companion PR (3.6).
9. Post-roll: tests 28-29 on the issue.

## Appendix A: verification record

Pinned to `origin/main` at `70db2e1059fdf8f35751b17806496cb91f427b09`, read in a detached worktree cut with an explicit start-point.
Every `file:line` in the body was re-read from that worktree. The parked `run/repo` checkout (HEAD `agent/858-impl-wip`) was never grepped.

**Executed live, not inferred.**

| Claim | How |
|---|---|
| codex 401s with no credential | `codex exec -s read-only --skip-git-repo-check "reply OK"` in this container |
| `CODEX_HOME` is set by nothing | `grep -n 'CODEX_HOME' docker/docker-entrypoint.sh` -> no match |
| One `BRAIN_ENV` assignment retargets the whole flow | probe with `PYTHONPATH` pinned to the main worktree, printing `IMPORTED FROM:` to prove which copy ran |
| An exported variable reaches the manager through `gosu ... env` | `FOO=bar sh -c 'export FOO; env HOME=/x env'` still prints `FOO=bar` |
| A stale API-key `auth.json` shadows subscription OAuth | isolated `CODEX_HOME` holding only `{"OPENAI_API_KEY":"sk-stale-apikey-probe"}`; `codex login status` prints "Logged in using an API key", exit 0 |
| `codex login --device-auth` deletes `auth.json` itself, at start | same isolated home, checked while the device flow was still pending: the file is already gone |
| `codex logout` destroys a file-level symlink, and a boot-time `ln` then revives a revoked credential | measured against the baked `codex-cli 0.144.5`; this is why D4's directory shape is the one that is right, not the smaller file-level link |
| codex writes PATH-alias helpers under `CODEX_HOME` on every invocation | measured with `CODEX_HOME` **outside** a temp dir; under a `/tmp` `CODEX_HOME` codex declines with "Refusing to create helper binaries under temporary dir", which is why an earlier round read this as non-reproducible |
| `${CODEX_HOME}` footprint | ~105 MB after one `codex exec`; `~/.codex` 107 MB live; `/data` 8.9 GB free of 15 GB |
| Zero gateway-brained teams | per-file sweep over `git ls-tree -r origin/main` of `moda-labs/moda-agents`, printing each `agent.yaml`'s `brain:` block: 7 files, `kind` only, `base_url` in none |
| Unit baseline | `.venv/bin/python -m pytest tests/test_auth_bootstrap.py -q` -> 78 passed in 2.35s |

**The `--device-auth` credential shape, observed.**
D8 drops the "obtain a fixture before merge" blocker because the fixture now exists.
This box is device-logged-in (`~/.codex/auth.json`, `auth_mode: "chatgpt"`). Keys only, no values:

```
{auth_mode: str, OPENAI_API_KEY: null, tokens: {id_token, access_token, refresh_token, account_id}, last_refresh: str}
```

Both arms are correct on it, re-run verbatim: the entrypoint predicate `codex_auth_uses_api_key` reads **false** and leaves the file alone, exactly as the comment at `:348-353` predicts for a null `OPENAI_API_KEY` alongside `tokens`; and `python -m bobi.auth_bootstrap credential-status codex ~/.codex/auth.json` returns "credentials valid: refresh token is present", rc 0.
The real credential was read for its key names and never written: `codex login status` still reports logged in through ChatGPT.

**Line-number drift to watch at implementation time.**
`origin/main` moved during review, and `bobi/cli.py` took +206/-68 from local service supervision (#1098).
The body's `bobi/cli.py` citations are the current ones: `:514-525` (`_refuse_runtime_lifecycle`), `:808-809` (`--timeout`), `:827-828` (the pre-check and its message), `:831` (`run_bootstrap`), `:1221`/`:1265` (the two wired call sites).

**Not verified here.**
The `moda-labs/moda-agents` citations in 3.6 are in another repository and were read from its `origin/main` rather than from a pinned worktree in this tree.
