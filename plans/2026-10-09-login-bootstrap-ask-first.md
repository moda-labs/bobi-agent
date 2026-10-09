# Ask-first `login-bootstrap`: one login primitive for the brain and for CLI tools

> **Status:** Draft, awaiting Gate 1 approval from Zach.
> No implementation until approved.
> **Tracking issue:** moda-labs/bobi-agent#958 · **Spec PR:** #959 (draft) · **Written:** 2026-10-09
>
> **Supersedes** [`plans/2026-08-05-codex-subscription-auth-in-flight.md`](2026-08-05-codex-subscription-auth-in-flight.md).
> That plan posted the device code immediately and treated the brain flow and the tool flow as separate shapes.
> Zach's 2026-10-09 decisions replace both halves of that: ask first, and one shared behaviour.
>
> **Size:** ~55 lines across 7 files in `bobi-agent`, plus ~25 lines of prompt/doc text in `moda-agents`.
> The round-3 fold cut the entrypoint change from ~36 lines to 5 and added no new shell predicate (3.4).
> [Q4](#5-open-questions-for-zach) considers a ~40-line variant and rejects it.
>
> Every file:line below was read from a grep run against `origin/main` at `83bebe49` on 2026-10-09.
> [Appendix A](#appendix-a-verification-record-2026-10-09) is the verification record, including what was executed live.
>
> **Reviewed four times, every round committed verbatim beside this file.**
> Round 1: [`reviews/2026-10-09-958-review-1.md`](reviews/2026-10-09-958-review-1.md), verdict SOUND WITH FIXES, 15 findings, 3 blockers.
> Round 2: [`reviews/2026-10-09-958-review-2.md`](reviews/2026-10-09-958-review-2.md), verdict NOT READY on the round-1 fold, 14 findings, 1 blocker and 4 majors all introduced BY that fold.
> Round 3: [`reviews/2026-10-09-958-review-3.md`](reviews/2026-10-09-958-review-3.md), verdict NOT READY, 13 findings, 1 blocker and 7 majors, plus a tested answer to the simplification question.
> Round 4: [`reviews/2026-10-09-958-review-4.md`](reviews/2026-10-09-958-review-4.md), scoped to the round-3 fold delta, verdict **SOUND WITH FIXES**, 6 findings, **no blocker**, and it confirms the new section 3c (48 of 48 boot cells clean, against the round-2 shape's 31 of 48 defective).
> 48 findings folded across four rounds, none rejected.
> **Round 3 replaced section 3c's shape rather than patching it**, because that one block had taken a defect in every round from one cause (3.4).
> All four rounds single-model: `codex` 401s and `aichat` is unconfigured in this container, which is the gap this spec exists to close.
>
> **Round 5 is the first review by a model other than Claude, and it reopens Gate 1.**
> [`reviews/2026-10-09-958-codex-review.md`](reviews/2026-10-09-958-codex-review.md), codex `gpt-5.6-sol`, verdict **NOT READY**, 11 findings, 3 blockers, 7 majors, 1 minor.
> `codex` subscription auth started working on this box on 2026-10-09, so the single-model limitation stated above is lifted from round 5 onward.
> Folded in [section 9](#9-amendment-2026-10-09-round-5-codex-review-fold), which is **insertion-only**: sections 1 to 8 and Appendix A are byte-identical to `ba774d38`, and section 9 wins wherever it contradicts them.
> 10 findings folded, 1 retracted with evidence, and **4 new questions Q5-Q8 are unanswered**, so this spec is not ready for approval as written.
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
- The Claude `paste_back` flow calls it at boot already (`bobi/auth_bootstrap.py:677`).

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
`grep -rn 'conversations\.replies' .` over the whole tree matches exactly two lines, both in `tests/integration/test_channel_gateway.py`: `:86` is a test stub's dispatch table and `:251` is an assertion over that stub's recorded calls.
(`docs/RELEASE_RUNBOOK.md:389` reads `conversations_replies` with an underscore, which is a `venn` tool name in an operator recipe, not a Slack API call from this codebase.)
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
  So the thread condition is `fields.thread_ts == <the ask's ts>`, on both paths.
  It is an **addition** to the filters `_extract_code` already applies, not a replacement: the source check (`:486-487`) and the login-channel check (`:496-499`) stay, because the listener is registered on the workspace-wide app topic (`:543-546`) and still sees traffic from other channels.

  **The filter is on `fields.thread_ts` only. It must not also filter on the event type.**
  `threadTs` is read once, before any type branching (`event-server/core/src/adapters/chat-sdk-slack.ts:106`), and `fields.thread_ts` is set for every emitted type (`:166`).
  So `slack.thread_reply` is not the only type that can carry the ask's `ts`: a human who @mentions the bot *inside* the ask's thread is emitted as `slack.mention` with `fields.thread_ts` set, because `app_mention` takes the branch at `:115-116` before the dedup branch at `:117-118`.
  Under Z5 that message is "ready", so an event-type filter would reject a human reply the design must accept.
  Verification item 4 pins both directions.

  **What this actually excludes, stated correctly.**
  It is not loose channel chatter: the adapter already drops that.
  A channel `message` with no self-mention and no `thread_ts` falls through to `return { event: null }` (`:123-124`), and a channel `message` that *does* mention the bot is dropped as a duplicate of the `app_mention` copy (`:117-118`).
  The real exposure is narrower: an unrelated **thread reply** elsewhere in the login channel (`:121-122`, emitted as `slack.thread_reply`) or a **top-level @mention** of the bot in that channel (`:115-116`, `slack.mention`, no `thread_ts`).
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
Four are `def fake_spawn(home)` injections, defined at `:600`, `:674`, `:807` and `:1198` and passed at `:621`, `:692`, `:811` and `:1209`, of which three reach `:660` and `:807`'s asserts it is never called.
One monkeypatches the module attribute, `lambda home:` (`:1289`).
So three injected fakes plus one monkeypatch actually bind at `:660`, and all four are `(home)`-only.
The env override leaves `:660` byte-identical - confirmed in the same probe run - so no fake breaks.
`_spawn_login` re-reads `_active_spec()` internally (`:213`), so it picks the target up for free.

Two small edges, and one rule that replaces what looked like two edges.

**Scope both brain-credential guards to the brain.**
`run_bootstrap` carries two refusals, and both are arguments about the *brain's* credential path, not a tool's:

- The **gateway guard** (`:614-625`) raises for a gateway brain that is not Claude, or a Claude gateway with `ANTHROPIC_AUTH_TOKEN` set. Its own message says what it is about: "this gateway authenticates with gateway credentials in the runtime .env."
- The **shadow-env guard** (`:635-639`) raises when `os.environ[spec.shadow_env]` is set, which the override makes `OPENAI_API_KEY` for a codex target.

Gate both on `target is None`, as two guards in place rather than one hoisted block.
They are not adjacent and must not be made adjacent: `set_process_brain_from_config(cfg)` (`:626`), `spec = _active_spec()` (`:627`) and the credential short-circuit (`:630-633`) sit between them, and the shadow-env guard reads `spec.shadow_env`, so hoisting it up to join the gateway guard would move it above the binding it depends on.

```python
if cfg.brain_is_gateway and target is None and (...):   # :614-625, unchanged body
    raise RuntimeError(...)
...
if target is None and os.environ.get(spec.shadow_env):  # :635-639, in place
    raise RuntimeError(...)
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
It is now incompatible with the design: the director runs `login-bootstrap codex`, and the director is a manager descendant, which is exactly what that guard refuses (`bobi/cli.py:471-482`, `caller_is_manager_descendant` at `bobi/service.py:303`).
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
# On a codex BRAIN this is already ~/.codex: section 3b points that at the same
# directory, so the export just names it. On any other brain it moves codex's
# config dir onto the volume so a human-minted auth.json survives a roll.
# mkdir -p is required, not tidiness: codex exits 1 with "CODEX_HOME points to
# ..., but that path does not exist". chown because the warm-boot chown list
# (:375-378) does not cover this path. No `skills` object is created here,
# deliberately: see below.
mkdir -p "${DATA_DIR}/codex"
chown "${APP_USER}:${APP_USER}" "${DATA_DIR}/codex"
export CODEX_HOME="${DATA_DIR}/codex"
```

Three statements, no branch, no `ln`, no `rm -rf`.
Section 3b is untouched: no cleanup line, so no placement argument and no errexit-position argument.

**This is the round-3 shape. It replaces a branch-and-link shape that took a defect in every single review round.**
[Round 3](reviews/2026-10-09-958-review-3.md) found the class behind all of them, which is why this section is now three lines instead of thirty-six.

The earlier shape created `${DATA_DIR}/codex/skills`, and needed that one persistent path to be a **different kind of object per brain**: a real directory owned by 3b on a codex brain, a symlink out to the image HOME on any other.
Two writers, two required shapes, one path, on a volume that outlives the brain choice.
Every defect was an instance of that collision, not an independent bug:

| Found in | Instance |
|---|---|
| pre-review | `ln` over 3b's real directory: `cannot overwrite directory`, boot **abort** |
| pre-review | link placed before the codex block: `~/.codex` repointed under it, **symlink loop** |
| round 1 | skip-if-present guard: a claude boot inherits 3b's directory, the image's codex skills **silently lost** |
| round 2 | cleanup line inside `[ -d /opt/bobi/skills ]`: the loop returns on an image with no baked skills |
| round 3 | `~/.codex` is itself a link to the volume on a persisted overlay, so 3c links the path **to itself** |

Round 3's instance (its R3-F1) is the one that settles it.
`${HOME}/.codex` is written in exactly one place, `:527-531`, inside the codex-brain block, and nothing on a non-codex boot ever restores it to a real directory.
So a rootfs that has booted codex once keeps `${HOME}/.codex -> ${DATA_DIR}/codex` forever, and `ln -sT "${HOME}/.codex/skills" "${DATA_DIR}/codex/skills"` is then a link to itself: measured `rc=0`, no log line, `readlink -f` exit 1.
A same-machine restart keeps that overlay, and the entrypoint states the distinction itself at `:400-401` ("a fresh image rootfs each deploy ships the real dir; a same-machine restart already has the link").
The brain can change without a new rootfs, because it is read from the installed team on the volume when `BOBI_BRAIN` is unset (`:276-278`).
The earlier harness recreated `${HOME}` on every simulated boot, so its "correct on every boot order" claim never saw this axis.

Deleting the link makes all five instances unreachable rather than fixed.

**Nothing reads `${CODEX_HOME}/skills`, which is what makes deleting it safe.**

- `grep -rn 'codex_home()' --include=*.py .` returns six sites (`bobi/brain/codex.py:387`, `bobi/brain/codex_config.py:51`, `:181`, `:208`, `bobi/chat_history.py:317`, `bobi/brain/instructions.py:123`), and none of them touches `skills`.
- The only in-repo references to `~/.codex/skills` are gstack's installer (`bobi/tool_library/gstack/tool.yaml:33`, `:35`, `:51`), all three by literal `$HOME` path, which the export does not move.
- codex builds its own: one `codex exec` into a fresh `CODEX_HOME` creates `skills/.system`. On a claude brain that now lands on the volume, durable.

**And the link never delivered what the old 3c comment claimed it did.**
That comment said "any other brain needs a link out to the image's own codex skills", and the boot matrix asserted a baked team skill was reachable through it.
No build step puts a `/opt/bobi/skills` entry under `~/.codex/skills`.
The renderer links `BAKED_SKILLS` into `~/.claude/skills` and nowhere else (`bobi/build_render.py:220-221`), and `grep -rn 'BAKED_SKILLS' bobi/ Dockerfile hatch_build.py` returns only `build_render.py`.
On the live image all 50 `~/.codex/skills` entries point into `/home/bobi/dev/gstack/.agents/skills/`, and `find ~/.codex/skills -maxdepth 1 -lname '/opt/bobi/skills*'` returns none.
So the old verification item 12 was a test that fails on a correct implementation, which would have been weakened during implementation, taking the shape's only guard with it.

**The cost of dropping the link, stated.**
On a claude-brained machine that has never booted codex, `codex exec` no longer sees gstack's codex-side skill links.
That is the whole loss.
Nothing in the fleet requires them, gstack's own installer deliberately prunes six of them (`bobi/tool_library/gstack/tool.yaml:51`), and the eng-team codex brief already instructs codex not to read skill definitions.
It does falsify one sentence of tool-brief text, `bobi/tool_library/gstack/guide.md:7-8` ("its skills linked under `~/.claude/skills/` (and `~/.codex/skills/`)"), which is in this repo and so is one more line in this PR, not a companion edit.
And after any codex boot the volume keeps 3b's real `skills` directory, which 3c no longer `rm -rf`s, so codex-on-a-claude-brain ends up with more skills than the old shape allowed.

**If you want the link kept**, the minimum correct form adds the mirror of 3b's own repair, because the link *target* can be a stale brain link:

```sh
if [ "${ENTRYPOINT_ENGINE}" != "codex" ]; then
  [ -L "${HOME}/.codex" ] && rm -f "${HOME}/.codex"   # a previous codex boot left a link to the volume
  mkdir -p "${HOME}/.codex/skills"
  rm -rf "${DATA_DIR}/codex/skills"
  ln -sT "${HOME}/.codex/skills" "${DATA_DIR}/codex/skills"
fi
```

That costs four extra lines, a `rm -rf` on the volume, a three-axis boot matrix as a standing test, and a brain switch that discards whatever the previous brain left there.
It buys gstack's codex-side skill links on a machine that has never booted codex.
Recommended against; say so if you want it.

The export reaches every process.
`as_app` and the final `exec` use `gosu ... env VAR=... cmd` (`:416`, `:637`), and `env` **adds** to the inherited environment rather than replacing it.
`credentials_path()` then resolves to `/data/codex/auth.json` for free, because the codex spec declares `credentials_dir_env="CODEX_HOME"` (`bobi/auth_bootstrap.py:112`).

Four follow-on edits the export forces:

- **`docker/docker-entrypoint.sh:547` - `materialize_codex_api_key_auth "${HOME}/.codex"` -> `"${CODEX_HOME}"`, plus a non-clobber guard.**
  The retarget on its own is not mechanical, it is destructive.
  `:547` is the non-codex-brain arm of the `api_key`-mode block (`:538`, `:546-548`), so after the retarget a claude-brained team with `BOBI_AUTH=api_key` and `OPENAI_API_KEY` set rewrites `/data/codex/auth.json` from the env var on **every boot**, overwriting a human-minted subscription credential on the volume.
  The cost is a credential that cannot survive a roll: every boot destroys it, so every roll needs a fresh human login.
  On `origin/main` it is worse than that, because `:635-639` refuses to re-mint while the key is set, but this spec scopes that refusal to `target is None` (3.3) and the on-demand trigger always passes a target, so the re-mint path stays open.
  Leaving `:547` on `${HOME}/.codex` is not the fix either: under the export codex reads `${CODEX_HOME}/auth.json`, so the api-key file would be written where codex no longer looks and an `api_key`-mode team's codex would 401 with no credential at all - the exact failure #958 exists to close, inflicted on a different team.
  Both halves are needed: retarget the write, and refuse to overwrite an OAuth file.

  **Use the validator the repo already has. Do not add a shell predicate.**
  Round 2's fold added `codex_auth_is_oauth`, a new `python - <<'PY'` heredoc testing `data.get("tokens")` truthiness.
  [Round 3](reviews/2026-10-09-958-review-3.md) (its R3-F5) rejected it, and the reason is the standing bar: it would be the **third** definition of "usable codex credential" in the tree, and the one that is wrong.
  The existing authority is `subscription_credentials_status(path, "codex")` (`bobi/auth_bootstrap.py:162-171`), already reachable from this very script as `as_app python -m bobi.auth_bootstrap credential-status codex <path>` and already called at `:570-571`.
  Measured against it, with `PYTHONPATH` pinned to the `83bebe49` worktree:

  | `auth.json` state | `credential-status codex` | `codex_auth_is_oauth` |
  |---|---|---|
  | OAuth, refresh token present | rc=0 `refresh token is present` | oauth |
  | api-key shaped | rc=1 `tokens is missing or malformed` | not oauth |
  | truncated | rc=1 `credential JSON is malformed` | not oauth |
  | `{"tokens":{"refresh_token":""}}` | rc=1 `refresh token is missing or blank` | **oauth** |
  | absent | rc=1 `credential file is missing` | not oauth |

  The last row is the defect.
  An OAuth-shaped file with a blank refresh token is unusable, and `codex_auth_is_oauth` pins it in both modes: `api_key` mode permanently refuses to materialize over it, and the widened sweep permanently refuses to clear it.
  Round 2's own argument for the positive predicate ("sends every non-OAuth state - absent, api-key, garbage - down the materialize path, which is the recovery") enumerated four states and missed the fifth.
  The validator gets all five right, so the guard becomes:

  ```sh
  else
    # Never turn OPENAI_API_KEY into Codex auth on top of a durable OAuth
    # credential on the volume (#958). The mirror of the sweep at :553-563.
    # The chown first: `as_app` reads as ${APP_USER}, and an unreadable file
    # is "invalid" to the validator, which would send us down the overwrite arm.
    chown "${APP_USER}:${APP_USER}" "${CODEX_HOME}/auth.json" 2>/dev/null || true
    if as_app python -m bobi.auth_bootstrap credential-status codex \
        "${CODEX_HOME}/auth.json" >/dev/null 2>&1; then
      log "Leaving durable Codex OAuth auth file intact; not materializing OPENAI_API_KEY"
    else
      materialize_codex_api_key_auth "${CODEX_HOME}"
    fi
  fi
  ```

  That is one code path for "is this codex credential usable", and it deletes a heredoc rather than adding one.

  **The one cost of going through `as_app`, and the line that pays it.**
  `codex_auth_uses_api_key` is called inline at `:559` and so runs as **root**; `as_app` is `gosu "${APP_USER}" env ...` (`:414-420`), so the validator reads as `bobi`.
  `subscription_credentials_status` maps `OSError` to invalid (`bobi/auth_bootstrap.py:136-137`), so "cannot read" and "not a usable credential" are the same answer, and both arms then go the wrong way on a root-owned mode-600 OAuth file: `api_key` mode overwrites it and the subscription sweep deletes it every boot.
  No entrypoint path produces that state (`materialize_codex_api_key_auth` ends in `chown -R` at `:333`, `login-bootstrap` and `codex login` run via `as_app`, and the first-boot `chown -R "${DATA_DIR}"` at `:372` chowns toward `bobi`), so this is a hardening line rather than a bug fix.
  The reachable route is an operator taking the fallback the code itself prints (`bobi/auth_bootstrap.py:719`, `fly ssh console` then `codex login --device-auth`) in a root shell with `CODEX_HOME` set.
  The `chown` above closes it, and also closes the gap that 3c chowns the directory but never the file.
  Round 4 found this (its R4-F4).

  **Why a guard is needed at all, measured.**
  The obvious shape is `[ -f ... ] && ! codex_auth_uses_api_key ...`, reusing the one shell predicate that already exists.
  `codex_auth_uses_api_key` (`:336-355`) exits 1 for a file that does not parse (`:344-347`), so a *truncated* `auth.json` reads as "not an api-key file" and the negated guard preserves it, logging that it kept an OAuth credential.
  The writer that can produce that state is a plain non-atomic `write_text` (`:330`).
  Executed: with a truncated file, the negated guard leaves it in place in `api_key` mode and the `:553-563` sweep leaves it in place in subscription mode, while `codex login status` reports `Error checking login status: EOF while parsing a string`.

  **Severity, corrected in round 3.**
  Round 2's fold said "subscription mode self-heals regardless, because section 4's `credential-status` check (`:570-571`) reports the file invalid and `login-bootstrap` re-mints over it".
  That is false on the arm this bullet is about.
  Section 4 checks the **brain's** credential: `credential_path="${BRAIN_CRED_DIR}/${BRAIN_CRED_FILE}"` (`:569`) with `${ENTRYPOINT_ENGINE}` (`:570-571`), and on a claude brain that is `${CLAUDE_CONFIG_DIR}/.credentials.json` (`:100-102`), while `:575` is a bare `login-bootstrap` that logs in the brain only.
  So on a non-codex brain nothing at boot ever looks at `/data/codex/auth.json`.
  The correct statement: in subscription mode the **widened sweep** is the boot-time recovery, and without it there is no boot-time recovery in either mode.
  Recovery otherwise needs a worker 401, a director dispatch and a human reply, and Z1 says a shell cannot be assumed.
  Verified for the other states: `codex login --with-api-key` writes `{"auth_mode":"apikey","OPENAI_API_KEY":...}` with no `tokens`, so a stale api-key file is still replaced rather than pinned.
- `docker/docker-entrypoint.sh:557` - `codex_dir="${HOME}/.codex"` -> `"${CODEX_HOME}"`, so the subscription sweep at `:559-562` reads the file codex actually uses.
  This edit is both required and the only boot-time recovery path for the hazard above: in subscription mode it deletes an unusable `auth.json` from the volume so OAuth can be minted.
  Widen its predicate with the same validator, and keep a `-f` precondition:

  ```sh
  chown "${APP_USER}:${APP_USER}" "${codex_dir}/auth.json" 2>/dev/null || true
  if [ -f "${codex_dir}/auth.json" ] \
     && ! as_app python -m bobi.auth_bootstrap credential-status codex \
        "${codex_dir}/auth.json" >/dev/null 2>&1; then
    log "Subscription mode: removing unusable Codex auth file so OAuth can be used"
    rm -f "${codex_dir}/auth.json"
  fi
  ```

  The same `as_app` readability caveat applies here, and it needs its **own** `chown` line: `:538`'s api-key block and `:553`'s sweep are mutually exclusive on `BOBI_AUTH`, so in subscription mode the api-key arm's `chown` never runs.

  **The `-f` precondition and the log reword are both required, and round 2's fold dropped both.**
  A negated predicate with no `-f` test enters when there is *no* file at all, and the existing message at `:560` then claims a deletion that did not happen.
  Measured: in subscription mode with `auth.json` absent, the widened sweep logs "removing Codex API-key auth file so OAuth can be used" and deletes nothing.
  That fires on every boot of every subscription-mode machine before its first login, which is every machine, and it destroys the line's value as the audit signal that a credential *was* deleted.
  The reword is needed because the predicate no longer means "is an api-key file".

  **This widening inverts the sweep's failure mode, from conservative to destructive.**
  `codex_auth_uses_api_key` exits 1 on anything it cannot classify, so today an unrecognised file is never deleted.
  Under the widened predicate an unrecognised file is deleted on every boot.
  That is the right trade, because an unusable file on the volume otherwise outlives every boot in both modes, but it is a new destructive path and it needs the test that verification item 11 now carries.
  It also raises the stakes on the one thing this spec could not verify: codex's `--device-auth` write shape was never observed, and if a real OAuth `auth.json` can lack a truthy `tokens` the sweep would destroy it every boot.
  Re-confirm on the first real login (verification 15) before this ships.
- **Not in scope, but say it:** the guard above protects the non-codex arm (`:546-548`) only. The codex-brain arm at `:544` passes `${BRAIN_CRED_DIR}`, which is the same `${DATA_DIR}/codex` path (`:89`), so a codex-brained `api_key` team still rewrites its one credential from the env var on every boot. That is pre-existing behaviour and arguably what an `api_key` team wants; it is named here so section 6's claim can be scoped honestly rather than read as universal.
- **`bobi/tool_library/codex/tool.yaml` - a retarget AND the same guard.** **Superseded plan's validity review, its F12.**
  Its `success:` (`:6`) and `fix:` (`:8`) reference codex's home by literal path 6 times (two `~/.codex`, one `~/.codex/auth.json`, three `pathlib.Path.home()/".codex"` constructions) and `grep -n 'CODEX_HOME' bobi/tool_library/codex/tool.yaml` returns nothing.
  Under the export, that check would read the wrong directory, miss a real credential and fail closed.
  **The retarget alone re-opens the clobber the guard above just closed**, which round 2's fold did not state and [round 3](reviews/2026-10-09-958-review-3.md) (its R3-F8) caught.
  `success:`'s `elif` branch *writes*, unconditionally, every time the preflight runs:

  ```
  elif [ "${BOBI_AUTH:-api_key}" != "subscription" ] && [ -n "${OPENAI_API_KEY:-}" ]; then
    mkdir -p ~/.codex && python3 -c '... p=pathlib.Path.home()/".codex"/"auth.json"; p.write_text(...)'
  ```

  Point that at `${CODEX_HOME}` with no guard and an `api_key`-mode team's preflight overwrites the human-minted OAuth credential on the volume, with none of the protection `:547` just gained.
  So this edit is two changes, not one: honour `CODEX_HOME`, and skip the write when a usable OAuth credential is already there.
  `tool.yaml:6` already carries its own fourth definition of the credential shape in its subscription branch (`data.get("OPENAI_API_KEY") and not data.get("tokens")`); fold that into the same `credential-status` call rather than adding a fifth.
  Latent for eng-team, which declares no `tool_library:`; fail-closed and then destructive for the next team that does.
  It is this change's own breakage, so it is in scope.
- `bobi/tool_library/codex/guide.md:42` - says the tool "materializes `~/.codex/auth.json` from `OPENAI_API_KEY` before launch", which the export falsifies. One line, same change.
- `bobi/brain/codex_config.py:46-47` - **superseded plan's validity review, its F16.** The comment says "The entrypoint symlinks ~/.codex at the durable volume, so writing there persists", which is true only on a codex brain. One line; this change makes it unconditionally true.

And one stale comment this change falsifies: `docker/docker-entrypoint.sh:484` still reads "codex has no config-dir override", which stopped being true when `eb90538` landed `credentials_dir_env="CODEX_HOME"`.

**The export is a process-wide switch, so here is everyone who flips with it.**
`codex_home()` (`bobi/brain/codex_config.py:51-56`) reads `CODEX_HOME`, and it has five callers beyond `credentials_path()`:

| Caller | Brain-gated? | Effect on a claude brain |
|---|---|---|
| `bobi/brain/codex_config.py:181`, `:208` | n/a, internal to codex config | Config writers; only reached via `codex.py:387` below. |
| `bobi/brain/codex.py:387` | Yes, by construction | Inside `CodexBrain.make_session`, behind `:386`'s `if declared is not None:`. There is no literal `engine == "codex"` test there; the gate is the class. No change on a claude brain. |
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
| **Old change A1** - a new gateway refusal guard for a tool target | No new guard needed. The *existing* guard at `:614-625` just gets scoped to `target is None`. |
| **The `${DATA_DIR}/codex/skills` link, its `!= "codex"` gate, its `rm -rf`, and the 3b cleanup line** | Cut in the round-3 fold. One persistent path that had to be two kinds of object took a defect in every review round; nothing in the tree reads `${CODEX_HOME}/skills`, and the link never exposed the baked team skills it claimed to (3.4). Section 3c is now three statements with no branch. |
| **A `codex_auth_is_oauth` shell predicate** | Cut in the round-3 fold. `python -m bobi.auth_bootstrap credential-status codex` already exists, is already called from this script at `:570-571`, and is correct on a fifth state the new predicate got wrong (3.4). |
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
- **Option C (recommended): bounded at boot with a long timeout, re-posting into the existing thread.** Block for hours rather than 600s, and on timeout exit non-zero as today, but have the next boot reply *into the ask's existing thread* instead of opening a new one. One ask in Slack, the machine still cycles, and it stays observable. It needs somewhere to remember the ask's `ts` across boots, and the durable volume the rest of this spec already introduces is that place.
**One location, one file per target**, named by path rather than by `${CODEX_HOME}` so it holds under either Q4 shape: `${DATA_DIR}/codex/.login-ask-<target>`, with `<target>` being the brain kind for the bare call.
Round 2's fold said "`${DATA_DIR}/codex` for a tool and the brain credential dir for the brain", which collides on a codex brain: there the brain credential dir **is** `${DATA_DIR}/codex` (`docker/docker-entrypoint.sh:89`), so one slot would hold two flows' asks and the invalidation rule would clear the other flow's.
That invalidation rule is also required, or a login months later replies into a dead thread: clear the stored `ts` once the login succeeds, and treat it as absent if the post into it fails.
3.4's 3c no longer `rm -rf`s anything under `${DATA_DIR}/codex`, so the file is safe where it sits.
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

3.4 moves the **directory** (`export CODEX_HOME=/data/codex`), which is what redirects the five other `codex_home()` consumers.
It no longer creates a `/data/codex/skills` object: the round-3 fold deleted that, which is what removed the defect class behind both round-1 blockers.
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

**And `codex logout` destroys it, which is what settles this question.**
Round 2 tested the unlink paths that the first pass of this question did not, against the same binary:

| What | Result |
|---|---|
| `codex logout` with the link in place | `Successfully logged out`; **the link is gone**, and the credential is still on the volume |
| Re-login after that logout | writes a **real file** on the ephemeral overlay, not the volume; the volume still holds the logged-out credential |
| Next boot, re-running `ln -sfnT` | the link is recreated over the stale volume file and `codex login status` reports `Logged in` **with the credential the human revoked** |

So under the file-level shape a logout is not durable, and a boot resurrects a revoked credential.
The directory shape gets both right for free: `~/.codex` *is* the volume, so `codex logout` deletes the real file and a re-login writes the real file.
Closing that gap in the file shape needs a logout hook plus a rule that the boot-time link must not revive a deliberately removed credential, which is more machinery than the directory shape costs.
That inverts this question's own simplicity argument.

Checked and **not** a problem with the file shape: the self-referential `auth.json` case on a codex brain, where `${HOME}/.codex` is already the volume. With the shape's own `!= "codex"` gate bypassed, GNU `ln` refuses with "are the same file" and exits 1, so under errexit it is a loud boot abort rather than silent credential loss.

**What the file shape would delete from this spec, re-scored after the round-3 fold.** Less than it used to.
The skills-link hazards are no longer a difference between the two shapes: 3.4 no longer creates a `/data/codex/skills` object either (round 3's R3-F1), so that whole argument is gone from both columns.
What is left is the `tool.yaml` and `guide.md` changes (`~/.codex/auth.json` stays the right literal path), the `codex_home()` blast radius in 3.4, and two of the "accepted and stated" items in section 6 (codex's ~105 MB of state stays on the overlay, and `codex exec` keeps seeing gstack's codex-side skills).

**What it does not simplify, stated plainly.** Three things survive unchanged:

- The non-clobber guard on the api-key materialization is still required. `:547` writes to `${HOME}/.codex`, which now *is* the durable file through the link, so the clobber hazard is identical.
- The sweep at `:557` still has to be re-pointed, for a new reason: `rm -f "${HOME}/.codex/auth.json"` would delete the **link** and leave the api-key file on the volume, where the next boot's `ln -sfnT` would re-expose it. It must target `${DATA_DIR}/codex` on a non-codex brain.
- One new line is needed that the directory shape got for free: `materialize_codex_api_key_auth` runs as root and ends with `chown -R` on its argument, and `chown -R` does not follow the `auth.json` symlink, so a file created through the link on first boot stays root-owned. The chown must name the resolved path.

- **It needs the same placement constraint as 3.4**, between `:532` and `:538`, for a different reason: if 3c ran after `:547`, the api-key materialization would write a real file on the overlay and 3c's `ln -sfnT` would then replace it with a link to a possibly absent volume file, leaving an `api_key`-mode team with no credential where codex looks.
- **Q1's Option C would have no `${CODEX_HOME}` to persist the ask `ts` under**, since this shape exports no env var. Option C names `${DATA_DIR}/codex` for that reason, which exists either way, so the two recommendations have to be read together.

**Recommendation: keep the directory shape (3.4 as written). Do not take the file-level symlink.**
This reverses the recommendation this question carried when it was first drafted, and the reason is the logout evidence above.
The round-3 fold strengthens it further: the directory shape's own skills-link hazards are deleted rather than guarded, so the file shape's main remaining selling point was already conceded.
What the file shape still trades for is a credential-revocation bug, and reviving a credential a human revoked is a worse class of defect than a claude-brained machine not seeing gstack's codex-side skill links.
House precedent agrees (`CLAUDE_CONFIG_DIR`, and 3b's own `~/.codex` -> volume on a codex brain), and the directory shape is now tested across every boot order with the image's skills directory present and absent.
The question stays in the spec rather than being deleted, because it is the one place a reader would reasonably ask "why move the whole directory", and the answer is now on the record with the measurement behind it.
Overrule this if you want the smaller diff and will accept the logout gap.

## 6. Scope

**In scope.**

- `bobi/auth_bootstrap.py` - ask-first for both flows, thread-anchored posting, human-only reply filter, one shared wait function, the `target` parameter, and **both** brain-credential guards scoped to the brain (gateway `:614-625` and shadow-env `:635-639`).
- `bobi/cli.py` - the optional `<tool>` argument and a target-aware pre-check.
- `docker/docker-entrypoint.sh` - section 3c (three lines, no branch), the two re-pointed writers, the non-clobber guard on the api-key materialization, the widened sweep predicate with its `-f` precondition and reworded log line, one `chown` of `auth.json` in each of those two mutually exclusive arms, the stale comment at `:484`. Both predicates reuse `python -m bobi.auth_bootstrap credential-status`; **no new shell predicate, and no change to section 3b** (3.4).
- `bobi/tool_library/codex/tool.yaml` - honour `CODEX_HOME`, **and** skip the `elif` write when a usable OAuth credential is already present (F12, round 3's R3-F8).
- `bobi/tool_library/codex/guide.md:42` - the `~/.codex/auth.json` claim the export falsifies.
- `bobi/tool_library/gstack/guide.md:7-8` - the `~/.codex/skills/` claim that 3.4's dropped link falsifies.
- `bobi/brain/codex_config.py:46-47` - the stale comment (F16).
- `moda-labs/moda-agents` - the false preflight claim, the on-demand trigger prompt, the runbook line.

**Out of scope.**

- Any second login path, Slack-specific or otherwise (Z3).
- A codex `requires:`/preflight gate, a `doctor` codex row, a command rename, new flags or exit codes.
- Automatic re-login on credential expiry. codex OAuth has no refresh expiry (`bobi/auth_bootstrap.py:166-171`), so this is once per machine.
- Any other CLI tool. `aichat`, `gh` and `venn` are env-var/API-key based and already durable through `run/.env` on the same volume; `gstack` has no auth.

**Accepted and stated, not guarded.**

- **A codex tool login is permitted alongside an ambient `OPENAI_API_KEY`**, because the shadow-env guard is scoped to the brain (3.3). The on-disk OAuth `auth.json` wins, since codex reads only that file (`docker/docker-entrypoint.sh:535-536`), and the non-clobber guard in 3.4 keeps it that way across boots **on a non-codex brain**. On a codex-brained `api_key` team the pre-existing `:544` arm still rewrites the credential every boot; that arm is out of scope here (3.4).
- **On a claude-brained machine that has never booted codex, `codex exec` does not see gstack's codex-side skill links** (3.4). Nothing reads `${CODEX_HOME}/skills` in this repo, codex creates its own `skills/.system` there on first invocation, and the baked team skills never reached `~/.codex/skills` in the first place. Dropping the link is what removes the defect class section 3c kept producing. Moot under Q4's file-level shape, which leaves `~/.codex` where it is.
- `CODEX_HOME` moves codex's state to the volume. Measured on a fresh `CODEX_HOME` after one `codex exec`: ~105 MB, of which ~103 MB is a plugins git clone under `.tmp/`, plus four sqlite databases with WALs, `sessions/`, `shell_snapshots/` and `skills/.system`. Live `~/.codex` is 107 MB and `/data` has 8.9 GB free of 15 GB. Moot under Q4's file-level shape, which leaves everything but `auth.json` on the overlay.
- In `api_key` mode a plaintext key lands on the volume, alongside `GH_TOKEN`, `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET` and `LINEAR_API_KEY`, which already live there at mode 600.
- codex writes into `CODEX_HOME` on every invocation, so that content lands on the volume. `tests/integration/test_container_image.py:277` guards root-owned entries under `/home/bobi` but not under `/data/codex`. Section 3c's `chown` covers the directory on first boot, and with no `ln` left in 3c there is no sub-object that stays root-owned. No root-run `codex` invocation exists in the entrypoint today. Moot under Q4's file-level shape.
  **codex really does write PATH-alias helper binaries, and that is the whole reason this bullet exists.**
  Round 3 reported it as non-reproducible (its R3-F12) and the round-3 fold wrote that down; round 4 showed the negative was an artifact of measuring under `/tmp` (its R4-F3).
  Measured with `CODEX_HOME` outside a temp dir: every invocation creates `${CODEX_HOME}/tmp/arg0/codex-arg0<rand>/` holding `apply_patch`, `applypatch`, `codex-execve-wrapper` and `codex-linux-sandbox`, each a symlink to the codex binary, with the previous directory removed so there is no unbounded leak.
  Under a `/tmp` `CODEX_HOME` codex declines: "Refusing to create helper binaries under temporary dir".
  This repo already documents both the behaviour and the incident it caused: `tests/integration/test_container_image.py:277-284` says "codex drops PATH-alias helpers under `~/.codex` on any invocation" and that "the gstack `--host auto` bake died on root-owned /home/bobi/.codex exactly this way at the 0.47.0 fleet roll".
  That is what makes the root-ownership point load-bearing once this content moves to `/data/codex`, and `grep -n '\bcodex\b' docker/docker-entrypoint.sh` confirms the mitigating fact: no root-run `codex` invocation exists there today, only comments and log strings.

## 7. Verification plan

Unit, `tests/test_auth_bootstrap.py` (baseline today: 78 passed in 2.51s).

1. **Ask-first ordering, `device_poll`.** The ask is posted and the reply is consumed **before** `spawn_login` is called. Assert on call order; this is the Z2 guarantee and the one test that would catch a regression to post-first.
2. **Ask-first ordering, `paste_back`.** Same, for the brain flow. Z4.
3. **A bot reply is not "ready".** An event carrying `fields.bot_id` does not satisfy the wait; a following human event does.
4. **Only a human reply in the ask's own thread is "ready", on `fields.thread_ts` alone and never on the event type.** Written against `fields.thread_ts`, which is the field the filter reads (3.2(d)). Four legs:

   | Event | `fields.thread_ts` | Ready? |
   |---|---|---|
   | `slack.thread_reply` in the ask's thread | the ask's `ts` | **yes** |
   | `slack.thread_reply` in another thread | another `ts` | no |
   | `slack.mention`, top-level in the login channel | **absent** | no |
   | `slack.mention`, **inside the ask's thread** | the ask's `ts` | **yes (Z5)** |

   **Round 2's fold got this table wrong, and the wrong version drives a Z5 violation.**
   It asserted `fields.thread_ts` is absent for `slack.mention`, and added a fourth leg for `slack.dm` with no thread.
   [Round 3](reviews/2026-10-09-958-review-3.md) (its R3-F7) refuted both from the adapter.
   `threadTs` is read **once, before any type branching** (`event-server/core/src/adapters/chat-sdk-slack.ts:106`) and `fields.thread_ts` is set for **every** emitted type (`:166`).
   So a human who @mentions the bot *inside the ask's thread* takes the `app_mention` branch at `:115-116`, before the dedup branch at `:117-118`, and is emitted as `slack.mention` carrying the ask's `ts`.
   Per 3.2(d) that event is ready, and per Z5 it must be.
   The defect path is concrete: an implementer writing this test from the old table builds a `slack.mention`, finds the `thread_ts`-only predicate accepts it, and "fixes" the predicate with an event-type filter, which then rejects a human message in the ask's own thread.
   The `slack.dm` leg was also dead and is dropped, on both channel paths.
   On the legacy channel-name path `_extract_code`'s login-channel check compares channel ids (`bobi/auth_bootstrap.py:497-499`), `fields.channel` is Slack's raw id (`:129` -> `:162` in the adapter), and the login channel resolves to a `C...` id (`bobi/auth_bootstrap.py:297`, `:302`), so a `D...` DM never reaches the thread test.
   On a conversation-ref destination `legacy_slack_channel` is empty and `:498` is skipped entirely, so the DM is dropped one step earlier by `_conversation_matches` (`:488-491`): a DM anchors as `slack:<team>:dm:D...` (`event-server/core/src/conversation.ts:74-96`), which neither equals nor prefix-matches a `slack:<team>:channel:C...` expectation.
   The "no `thread_ts` at all" case stays, as leg 3, because a top-level @mention of the bot in `#bobi-eng-team` is the single most likely stray event while an ask is open.
5. **The device code is posted into the ask's thread**, not to the channel root. **Both post paths**, because they are different code and only the legacy one runs in production: legacy asserts `post_slack_message(..., thread_ts=<ask ts>)`, gateway asserts the destination ref is `<dest>:thread:<ask ts>`. One test over one path would ship the other unexercised.
6. **`target` retargets everything.** With a `claude` brain and `target="codex"`, the spawned command is `codex login --device-auth` and the credential path resolves under the codex spec's credential dir.
7. **`spawn_login` is still called with exactly one argument.** A fake with a `(home)`-only signature still binds when `target="codex"`. This is the assertion with content; "no injected fake breaks" would be a tautology while `:660` is byte-identical.
8. **Neither brain-credential guard refuses a tool target.** A Claude-gateway config with `ANTHROPIC_AUTH_TOKEN` set refuses `target=None` and permits `target="codex"`; the same for `OPENAI_API_KEY` set with `target="codex"`. The existing refusal test (`tests/test_auth_bootstrap.py:1220`) must still pass unchanged, because it drives `BRAIN_ENV` directly and so runs with `target is None`.
9. **`cli.py`'s pre-check is target-aware.** With Claude credentials present and `codex` requested, it proceeds instead of printing "already present".
10. **Unchanged:** `test_login_bootstrap_posts_only_to_the_configured_channel` (`tests/test_auth_bootstrap.py:1336`) must still pass. It pins #1009's non-caller-controlled destination.

Shell lane, runnable without docker (the 3.4 harness shape).

11. **The credential guard and the sweep, five states x both auth modes.** One table, because the two arms are the same decision read in opposite directions, and both are destructive.

    | `${CODEX_HOME}/auth.json` | `api_key` + `OPENAI_API_KEY` set | `subscription` |
    |---|---|---|
    | absent | materialized | no-op, **and no log line** |
    | api-key shaped | replaced | removed |
    | truncated | replaced | removed |
    | `{"tokens":{"refresh_token":""}}` | replaced | removed |
    | OAuth, refresh token present | **left intact**, with a log line | **left intact** |

    Round 2's fold scoped this item to `api_key` mode only, which left the widened subscription sweep untested, and subscription is the mode eng-team runs.
    The blank-refresh-token row is the one `codex_auth_is_oauth` fails in both modes; the truncated row is the one a `! codex_auth_uses_api_key` guard fails.
    The absent row's "no log line" assertion is the pin for the `-f` precondition.
12. **Boot-order matrix: every boot exits 0.** claude, codex, claude on one persisted `${DATA_DIR}`, and the mirror, with `/opt/bobi/skills` present and absent, and with `${HOME}` persisted across boots as well as recreated. The harness **must** persist `${HOME}`: a per-boot-fresh `${HOME}` is a `fly deploy`, not a same-machine restart, and it is the axis that hid round 3's blocker. Under the 3.4 shape as written there is nothing else to assert, because 3c creates no object whose kind can be wrong. If the skills link is kept instead (3.4's rejected variant), this item also has to assert `readlink -e "$CODEX_HOME/skills"` exits 0 on every boot, across all three axes.

Docker lane, `tests/integration/test_container_image.py` (`-m docker`, needs a built image).

13. **Claude brain, subscription.** `/data/codex` exists and is bobi-owned, `CODEX_HOME` resolves to it in the manager's environment, the credential path resolves to `/data/codex/auth.json`, and it all survives a restart.
14. **Codex brain boots clean.** Boot succeeds and the brain credential is intact. This is the regression pin for the abort reproduced in 3.4.
    The skills assertion is **conditional on the fixture having baked skills**, which neither docker-lane image does today: `/opt/bobi/skills` exists only under `if spec.run:` (`bobi/build_render.py:213`), the `image` fixture builds the bare base image (`tests/integration/test_container_image.py:90-116`) and `team_deps_image` renders a fixture declaring `build.run_root:` and no `build.run:` (`:162-172`, `tests/fixtures/team-deps-bake/agent.yaml:6`).
    So: if `/opt/bobi/skills` is present, `readlink -e "${CODEX_HOME}/skills"` exits 0 and the directory lists an entry from it; if absent, `${CODEX_HOME}/skills` does not exist and that is correct.
    Round 3 removed the same unsatisfiable assertion from item 12 and the round-3 fold reintroduced it here; round 4 caught it (its R4-F1).
    The oracle is `readlink -e`, not `-f`: `readlink -f` exits 0 on a dangling link (measured), so every earlier round used a signal that passes on a broken path.
    Adding a `build.run:` step to a docker fixture would make the assertion unconditional, and is the better fix if anyone wants the stronger pin.

Live, post-merge, owed on the issue as proof of work.

15. **One real login end to end.** `login-bootstrap codex`, a human reply in the thread, a real device code, authorize, then `codex exec -s read-only` returning 0 from a worker.
16. **Survives a restart.** Same credential after a machine restart, with no second login.

Items 1, 2, 3, 9, 15 and 16 are unchanged from the round-1 spec, and item 10 is its item 11 verbatim.
Items 4, 5, 6, 7, 8 and 13 were reworded in the round-1 fold; items 11 and 12 are new in round 2.
The round-3 fold rewrote items 4, 11, 12, 13 and 14: item 4's table was wrong about the adapter, item 11 was missing a mode and a state, item 12's skills assertion was unsatisfiable on a real image, and items 13 and 14 used `readlink -f` as a correctness oracle when it passes on a dangling link.
Items 1 and 2 look redundant and are not: different `spec.flow` branches (`bobi/auth_bootstrap.py:662` vs `:683`).

## 8. Implementation plan

Not to be started until Gate 1 is approved, and not before **Q1** is answered, which is the one question with no recommendation this spec can defend on its own.
Q2, Q3 and Q4 all carry a recommendation, and each recommendation is what sections 3 to 7 are written to: silence on them means take the spec as written.
Q4 is the one to read first anyway, because it is the only question whose answer changes the shape of a change rather than a value.

1. Tests 1-5 (ask-first, human-only, thread-scoped, both post paths) against the current `run_bootstrap`, failing.
2. Change 1 (3.2), including the `_wait_for_chat_event` factoring. Tests 1-5 green, and the 78 existing tests stay green.
3. Tests 6-9, failing. Then change 2 (3.3). Test 7 is the pin for the superseded plan's F11.
4. Change 3 (3.4), in whichever shape Q4 selects. Directory shape: section 3c (three lines), both re-pointed writers, the non-clobber guard and the widened sweep (both on `credential-status`, no new shell predicate), `tool.yaml` retarget plus its own guard, and the three stale comments (`docker-entrypoint.sh:484`, `codex_config.py:46-47`, `gstack/guide.md:7-8`) plus `codex/guide.md:42`. Then tests 11-14. **Do not reintroduce a `${DATA_DIR}/codex/skills` link**; 3.4 records why, and three rounds of defects came out of it.
5. Review gate, full test run, PR.
6. The `moda-agents` companion PR (3.5).
7. Post-roll: tests 15-16 on the issue.


## Appendix A: verification record, 2026-10-09

Pinned to `origin/main` at `83bebe49`.
Round 1 read `origin/main` in a detached worktree at `/tmp/wt-958-main`; round 2 re-read it at `/tmp/wt-958-main-r2`; rounds 3 and 4 re-read it at the same path and additionally read current `origin/main` at `70db2e10` in `/tmp/wt-r3-main`, all cut with an explicit start-point.
Every round-2 and round-3 citation below was re-read from a pinned worktree rather than carried over.
The parked `run/repo` checkout (HEAD `agent/858-impl-wip`) was never grepped.

**`bobi/cli.py` line numbers need re-reading before implementation.**
`origin/main` moved from `83bebe49` to `70db2e10` during review, and that diff is +206/-68 on `bobi/cli.py` (local service supervision, #1098).
The cited code is byte-identical in both SHAs, so nothing in this spec is wrong, but every `cli.py` offset is +43 to +94 off against current main: `login-bootstrap` `:713` -> `:807`, `--timeout` `:714-715` -> `:808-809`, the `credentials_exist()` pre-check `:733` -> `:827`, the `run_bootstrap` call `:737` -> `:831`, `_refuse_runtime_lifecycle` `:471-482` -> `:514-525`, its `stop`/`restart` wiring `:1129`/`:1195` -> `:1221`/`:1265`.
`bobi/service.py:303` did not drift.
`bobi/auth_bootstrap.py`, `docker/docker-entrypoint.sh`, `bobi/brain/` and `bobi/build_render.py` are byte-identical between the two SHAs, so every other citation holds on current main.
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
| ~~**The shape in 3.4 is correct on every boot order**~~ | **REFUTED IN ROUND 3.** The round-2 claim was: same 6 sequences, every boot exits 0, `readlink -f` resolves, every non-codex boot lists both skills. It held only because the harness recreated `${HOME}` every boot. See the round-3 rows below. |
| Bare `[ -L x ] && rm -f x` is errexit-safe | executed standalone and mid-block under `set -euo pipefail` on bash 5.2.37 (the entrypoint's interpreter, `:1`): both reached the next line, `rc=0` |
| **codex writes `auth.json` in place, not by rename** | `codex login --with-api-key` over a symlink whose target exists: `Successfully logged in`, link intact, content in the durable target. Through a **dangling** symlink: durable target created at mode `600`, link intact. `codex login status` through a dangling symlink: `Not logged in`, no crash. This is what makes [Q4](#5-open-questions-for-zach) answerable. |
| **The 3b cleanup line inside the `[ -d /opt/bobi/skills ]` guard re-opens the loop** | same harness with the directory absent. `claude, codex`: boot 2 exits 0 with no log line, `readlink -f` fails, and reading a skill through the path gives "Too many levels of symbolic links". ~~Moved to after `:488`, every sequence is clean with the directory present or absent.~~ **That second clause is REFUTED on the same two axes as the row above**: round 4 replayed the after-`:488` placement across 48 cells and found 31 defective. The cleanup line is cut; 3.4 keeps this row only as the history of why. |
| `/opt/bobi/skills` is conditional | created only when a team declares build `run:` steps (`bobi/build_render.py:213`); the entrypoint guards for its absence at `:394` and `:489`. ~~This is why the cleanup line's placement is load-bearing.~~ The cleanup line is cut. The conditionality still matters, for verification item 14's oracle and because neither docker-lane fixture declares `build.run:`. |
| **`codex logout` unlinks a file-level symlink** | `Successfully logged out`, link gone, credential still on the volume. A re-login then writes a real file on the overlay, and re-running `ln -sfnT` on the next boot reports `Logged in` with the revoked credential. This is what rejects [Q4](#5-open-questions-for-zach). |
| A self-referential `auth.json` link aborts rather than corrupts | GNU `ln` refuses with "are the same file", exit 1, credential intact |
| **A `! codex_auth_uses_api_key` guard pins a truncated `auth.json`** | the negated guard preserves it in `api_key` mode and the `:553-563` sweep preserves it in subscription mode, while `codex login status` reports `Error checking login status: EOF while parsing a string`. ~~Asserting `tokens` positively sends it down the materialize path instead.~~ That was round 2's conclusion; it is cut, because the positive predicate pins a blank-refresh OAuth file (the round-3 row below). `credential-status` is what both arms now use. |
| The bare AND line's errexit safety is position-dependent | standalone and mid-block reach the next line; as a function body's last command the script exits 1. ~~The spec's placement is mid-block.~~ Moot: no bare AND line remains in the change set, because the 3b cleanup line is cut. Retained because it is the argument against hoisting this logic into a function, which 3.4 records as a ruled-out alternative. |
| `chown -R` does not dereference the `auth.json` symlink | `-P` is the default; the target's ctime is unchanged after `chown -R` on the directory |
| `codex exec` writes into its own config dir at runtime | a fresh `CODEX_HOME` gains `skills/.system`, `installation_id`, `shell_snapshots` and sqlite state on one invocation. ~~which is why the `rm -rf` cost is "re-derivable" rather than "nothing does this"~~ The `rm -rf` is cut. This now matters for the opposite reason: it is why a claude brain needs no `skills` link at all (3.4). |
| `codex_auth_uses_api_key` classifies a codex-written api-key file | the file codex writes is `{"auth_mode":"apikey","OPENAI_API_KEY":...}`; the entrypoint predicate (`:336-355`) exits 0 on it, so the non-clobber guard still replaces a stale api-key file |
| Baseline suite | `pytest tests/test_auth_bootstrap.py -q` -> 78 passed in 2.51s |
| Live env | `BOBI_AUTH=subscription`, `BOBI_BRAIN=claude`, `BOBI_LOGIN_CHANNEL=#bobi-eng-team` (the legacy channel-name path), `CODEX_HOME` unset, `OPENAI_API_KEY` unset |

**Round 3, executed.**
Harness rebuilt from the real entrypoint text (`sed -n '319,355p'` for the two predicate functions, `'481,532p'` for 3b, `'534,551p'` and `'553,563p'` for the api-key block and the sweep) rather than hand-written, with `/opt/bobi/skills` rewritten into the sandbox root.

| What | Result |
|---|---|
| **The round-2 shape self-loops when the container overlay persists** | `claude, codex, claude` and `codex, claude, codex` with `${HOME}` persisted across boots: the non-codex boot after a codex one gives `rc=0`, no log line, `readlink -f` exit 1, and `${DATA_DIR}/codex/skills -> ${HOME}/.codex/skills -> ${DATA_DIR}/codex`. This is why 3c no longer creates the link. |
| `${HOME}/.codex` is never restored on a non-codex boot | written only at `:527-531`, inside the codex-brain block; `grep -n 'BRAIN_HOME_LINK\|\.codex' docker/docker-entrypoint.sh` finds no other writer |
| A brain switch needs no new rootfs | `:276-278` takes the brain from the installed team on the volume when `BOBI_BRAIN` is unset; `:400-401` states that a same-machine restart keeps the overlay |
| **The baked team skills never reached `~/.codex/skills`** | `grep -rn 'BAKED_SKILLS' bobi/ Dockerfile hatch_build.py` returns only `build_render.py`, which links them into `~/.claude/skills` (`:220-221`). On the live image `readlink ~/.claude/skills` -> `/opt/bobi/skills`, and `find ~/.codex/skills -maxdepth 1 -lname '/opt/bobi/skills*'` returns **0** of 50 entries |
| **Nothing reads `${CODEX_HOME}/skills`** | the six `codex_home()` sites touch no `skills` path; the only `~/.codex/skills` references in the tree are gstack's installer (`tool_library/gstack/tool.yaml:33`, `:35`, `:51`), all by literal `$HOME` path |
| **The three-line 3c is clean on every axis the old shape broke** | all 6 boot sequences x {`${HOME}` fresh, persisted} x {`/opt/bobi/skills` present, absent} x {`~/.codex/skills` present, absent}: `rc=0` throughout, no symlink created, so no loop and no dangling link is constructible. After a codex boot the volume keeps 3b's real `skills` directory and a later claude boot keeps it |
| `readlink -f` exits 0 on a dangling link | `readlink -f` -> prints the missing target, `rc=0`; `readlink -e` -> `rc=1`. Every earlier round used `-f` as the correctness oracle, so items 12 and 14 now use `-e` |
| **`codex_auth_is_oauth` pins an unusable OAuth file** | `{"tokens":{"refresh_token":""}}` reads as oauth to the round-2 predicate; `credential-status codex` rejects it (`refresh token is missing or blank`). Measured: `api_key` mode permanently refuses to materialize over it and the widened sweep permanently refuses to clear it |
| `credential-status codex` is correct on all five states | oauth `rc=0`; api-key, truncated, blank-refresh and absent all `rc=1` with distinct reasons. `IMPORTED FROM: /tmp/wt-958-main-r2/bobi/auth_bootstrap.py` |
| The widened sweep logs a delete that did not happen | subscription mode, `auth.json` absent: `rc=0`, nothing removed, and `:560`'s "removing Codex API-key auth file" still logged. Fixed by the `-f` precondition |
| Section 4 does not see the codex tool credential | `:569-571` reads `${BRAIN_CRED_DIR}/${BRAIN_CRED_FILE}` with `${ENTRYPOINT_ENGINE}`, which on a claude brain is `${CLAUDE_CONFIG_DIR}/.credentials.json` (`:100-102`), and `:575` is a bare `login-bootstrap` |
| **The adapter sets `fields.thread_ts` on every event type** | `threadTs` is read at `chat-sdk-slack.ts:106`, before type branching, and applied at `:166`. So a threaded `app_mention` emits `slack.mention` carrying the ask's `ts`, via the `:115-116` branch taken before the `:117-118` dedup |
| A DM never reaches the thread test | `_extract_code` drops it on the channel-id compare at `bobi/auth_bootstrap.py:497-499`; `fields.channel` is the raw `D...` id and the login channel resolves to a `C...` id (`:297`, `:302`) |
| `tool.yaml`'s `elif` write is unguarded | `bobi/tool_library/codex/tool.yaml:6` does `mkdir -p ~/.codex` then `p.write_text(...)` whenever `BOBI_AUTH != subscription` and `OPENAI_API_KEY` is set. Retargeting it to `${CODEX_HOME}` without a guard overwrites a minted OAuth credential |
| codex's real footprint | one `codex exec` into a fresh `CODEX_HOME`: ~105 MB, `.tmp/plugins` git clone ~103 MB, four sqlite DBs with WALs, `sessions/`, `shell_snapshots/`, `skills/.system`, `installation_id`. **No `bin` directory and no alias binaries.** Live `~/.codex` 107 MB; `/data` 8.9 GB free of 15 GB |
| codex tolerates a broken skills path | `codex exec` with `${CODEX_HOME}/skills` clean, dangling and looped: identical 401 in all three. Clean created `skills/.system`; the other two created nothing, silently |
| `bobi/cli.py` drifted, semantically unchanged | `diff` of the `login_bootstrap` block and of `_refuse_runtime_lifecycle` between `83bebe49` and `70db2e10`: identical. `git diff --stat 83bebe49..70db2e10 -- bobi/auth_bootstrap.py docker/docker-entrypoint.sh bobi/brain/ bobi/build_render.py`: empty |

**Round 4, executed.**
Scoped to the round-3 fold delta. Harness rebuilt independently and validated by first reproducing round 3's blocker and round 2's false pass before being trusted.

| What | Result |
|---|---|
| **The three-line 3c is clean on every axis; the round-2 shape is not** | 48 cells (6 brain sequences x `${HOME}` fresh/persisted x `/opt/bobi/skills` present/absent x `~/.codex/skills` present/absent). New shape: **0 defective**. Round-2 shape with the cleanup line after `:488`: **31 defective**, on two independent axes (persisted `${HOME}`, and `~/.codex/skills` absent even with `${HOME}` fresh) |
| **`mkdir -p` in 3c is load-bearing, not tidiness** | `CODEX_HOME=<missing path> codex login status` -> `Error loading configuration: CODEX_HOME points to "...", but that path does not exist`, rc=1. An export without the `mkdir -p` breaks `codex exec` on a claude brain's first boot |
| **codex DOES write PATH-alias helper binaries** | with `CODEX_HOME` outside `/tmp`: `${CODEX_HOME}/tmp/arg0/codex-arg0<rand>/{apply_patch,applypatch,codex-execve-wrapper,codex-linux-sandbox}`, all symlinks to the codex binary, on every invocation. Under a `/tmp` `CODEX_HOME`: "Refusing to create helper binaries under temporary dir". Round 3's negative result was a test-location artifact, and `tests/integration/test_container_image.py:277-284` already documents both the behaviour and the 0.47.0 fleet incident it caused |
| The export is correct on every codex-engine configuration | `BRAIN_CRED_DIR="${DATA_DIR}/codex"` at `:89-90` independent of `ENTRYPOINT_IS_GATEWAY` (the gateway branch at `:84-88` only sets `BRAIN_AUTH_TOKEN_KEY`), and `entrypoint_engine` maps both `codex` and `gateway-openai` to `codex` (`:74`). The three-line shape reads neither `ENTRYPOINT_ENGINE` nor `BRAIN_CRED_DIR` |
| The non-recursive `chown` is sufficient for the directory | first-boot `chown -R "${DATA_DIR}"` (`:372`) covers it once; the warm-boot branch chowns a narrow list that excludes it (`:375-378`), which is why 3c needs its own. Every later writer runs as `bobi` via `as_app` or chowns recursively itself (`:333`) |
| An unreadable `auth.json` reads as invalid | `credential-status codex` on a chmod-000 valid OAuth file -> rc=1 `credential file is unreadable`, while `[ -f ]` is still true. `subscription_credentials_status` maps `OSError` to invalid at `bobi/auth_bootstrap.py:136-137`. This is what the two `chown` lines in 3.4 pay for |
| **Deleting the skills link breaks nothing in the tree** | `grep -rn '\.codex/skills\|codex/skills' .` returns four lines: `bobi/tool_library/gstack/tool.yaml:33`, `:35`, `:51` and `gstack/guide.md:8`. All three `tool.yaml` references are literal `$HOME` paths and all three assertions are **negative** (six pruned skills must be absent), so the export does not move them. `grep -rn 'codex/skills' tests/` returns nothing |
| The "more skills after a codex boot" claim is true | live image: `/opt/bobi/skills` 51 entries, `~/.codex/skills` 50, **48 shared**. `/opt/bobi/skills` *is* gstack's claude-side install (`build_render.py:221` points `~/.claude/skills` at it before the `run:` steps), so 3b's surviving `${DATA_DIR}/codex/skills` covers almost everything the old link exposed. Precondition: an image with baked team skills |
| Neither docker-lane fixture has baked skills | the `image` fixture builds the bare base image (`tests/integration/test_container_image.py:90-116`); `team_deps_image` (`:162-172`) renders `tests/fixtures/team-deps-bake/agent.yaml`, which declares `build.run_root:` (`:6`) and no `build.run:`. So `spec.run` is empty and `build_render.py:213` is false. This is why verification item 14's skills assertion is conditional |
| `bobi/cli.py` drift, exactly | `git diff --numstat 83bebe49 70db2e10 -- bobi/cli.py` -> `206 68`, net +138 (4185 -> 4323 lines) |

**Could not verify.**

New or sharpened in round 3:

- **Whether a Fly machine restart preserves the container overlay in this deployment specifically.** The entrypoint asserts it does (`:400-401`) and its whole `~/.claude` idempotency block exists for that case, which is the evidence behind the round-3 blocker. Not observed on a live machine.
- **codex's `--device-auth` write shape**, which now matters more than it did. The widened sweep deletes any `auth.json` it cannot positively classify, so if a real OAuth file can lack a truthy `tokens` the sweep destroys it every boot. Re-confirm on the first real login (verification 15) before this ships.
- **Whether any out-of-repo consumer reads `${CODEX_HOME}/skills`.** The "nothing reads it" evidence that 3.4's simplification rests on covers this repo plus the gstack tool definitions it vendors. A `moda-agents` prompt or an operator recipe naming `~/.codex/skills` would not appear in that grep; the one in-repo brief that does is `bobi/tool_library/gstack/guide.md:7-8`, now in scope.
- **A live Slack round-trip for the threaded-`app_mention` case.** Round 3's correction to verification item 4 is read from the adapter source and the Python filter, not from a real event. Worth one minute during implementation.

Carried from earlier rounds:

- **An end-to-end login.** Needs a human to authorize a real device code. Each link is verified separately; the chain is not. This is verification 15.
- **The docker lane.** `-m docker`, needs a built image. No image build in this session.
- **That `/data/codex` behaves on the real volume.** Not created - that would be a live mutation during a spec step. The durability argument rests on `~/.claude -> /data/claude` on the same `/dev/vdc`.
- **Exact line counts.** Derived from the diffs this spec describes, not from an implementation.
- **A real thread reply arriving over the event bus.** The mechanism is verified by reading the code that already does it at boot and by the adapter fields it depends on; no live Slack round-trip was performed.
- **codex's OAuth device-flow write path specifically.** Q4's in-place-write evidence comes from `codex login --with-api-key`, the one write path reachable without authorizing a real device code. `--with-access-token` rejects a synthetic token before writing (`invalid agent identity JWT format`), so the `--device-auth` write was not observed. Both go through the same `auth.json` persistence, but that is read from behaviour, not from codex's source. Worth re-confirming on the first real login (verification 15) before relying on it.
- **Cross-model adversarial review.** Impossible here for the reason under review: `codex` 401s and `aichat` is unconfigured (`OPENROUTER_API_KEY`/`AICHAT_PLATFORM` unset). Single-model pass, stated as one.

## 9. Amendment, 2026-10-09: round-5 codex review fold

**Insertion-only.** Sections 1 to 8 and Appendix A are left byte-identical.
Where this section contradicts them, this section wins.
Round 5 is the first review of this spec by a model other than Claude, which is the gap line 25 above describes; that line is superseded by the header's round-5 line.

[Round 5](reviews/2026-10-09-958-codex-review.md), codex `gpt-5.6-sol` at reasoning effort high, read-only sandbox, spec sha `ba774d38`, main pinned at `70db2e10`.
Verdict **NOT READY**: 11 findings, 3 blockers, 7 majors, 1 minor.
Triaged against main in a worktree pinned at `70db2e10`: **10 folded as defects, 1 retracted, 4 new questions posed for Zach**.
Three blockers are design defects that four same-model rounds missed; none is a citation error.

| codex | Sev | Class | Verdict, against `70db2e10` |
|---|---|---|---|
| F1 | BLOCKER | **defect** | Confirmed. Post-then-subscribe inverts this repo's own contract (`bobi/inbox.py:444-447`, `bobi/events/client.py:207-211`); a lost reply is unrecoverable because replay is per deployment and `register` mints a fresh id (`event-server/core/src/core.ts:1453`). -> A1 |
| F2 | BLOCKER | **defect** | Confirmed. Fixed name `"login-bootstrap"` (`bobi/auth_bootstrap.py:543-546`) + same-name supersede that de-indexes the prior listener (`event-server/core/src/core.ts:1443-1451`). Q3's "cosmetic" is false. -> A2 |
| F3 | BLOCKER | **defect** | Confirmed. Root entrypoint (`docker/docker-entrypoint.sh:5-8`), app-owned volume (`:370-373`), `chown` dereferences; 3c widens main's codex-only `chown` (`:485-488`) to every brain. -> A3 |
| F4 | MAJOR | **defect** | Confirmed. Discord/WhatsApp are supported destinations (`bobi/auth_bootstrap.py:307-309`, `tests/test_auth_bootstrap.py:637-701`) with no `thread_ts` (`adapters/discord.ts:108-114`) and `threads: false` (`channels.ts:483`, `:644`). -> A4, remedy is **Q5** |
| F5 | MAJOR | **defect** + **scope fork** | Confirmed. `_active_spec` falls back to Claude on any unknown kind (`bobi/auth_bootstrap.py:117-120`), so a typo bypasses both guards: defect -> A5. The gateway-team policy half is **Q6**. |
| F6 | MAJOR | **defect** | Confirmed. Structural-only validation (`bobi/auth_bootstrap.py:162-171`) short-circuits at `:629-632` and `bobi/cli.py:827-829`, so the 3.5(b) recovery is a no-op on a revoked token. -> A6, remedy is **Q7** |
| F7 | MAJOR | **defect** | Confirmed. Channel-unset (`bobi/auth_bootstrap.py:648-653`) and event-server (`:521-526`) raises abort before Q1's timeout; Option C's state needs `atomic_write_text` + `file_lock` (`bobi/fsutil.py:1-17`, `:161-172`). -> A7, A/B/C stays Zach's |
| F8 | MAJOR | **defect** | Confirmed. Widened sweep (`docker/docker-entrypoint.sh:553-562`) depends on a shape item 15 defers post-roll (§8 step 7) while Appendix A demands it "before this ships". -> A8, remedy is **Q8** |
| F9 | MAJOR | **defect** | Confirmed. Timeout warns only (`bobi/auth_bootstrap.py:701-705`); `finally` terminates without wait, kill or group signal (`:706-712`) against a `start_new_session=True` child (`:220-224`). -> A9 |
| F10 | MAJOR | **defect** | Confirmed. Item 12 passes with 3c omitted (self-admitted in §7); no item asserts the outcome post. Rest derivative. Codex **retracts** the brief's "lanes are fictional" suspicion. -> A10 |
| F11 | MINOR | 1 **invalid**, 2 **defect** | CLI "stale citations" **declined**: Appendix A already lists the same seven offsets codex reproduces. `gstack/guide.md:7-8` as a fourth `~/.codex/skills` ref and the root-write window at `docker/docker-entrypoint.sh:324-333` are both real. -> §9.4 |

Totals: **10 defects folded, 1 claim retracted, 4 questions posed** (Q5-Q8). No finding was manufactured and none was dismissed without evidence.

Codex independently confirmed 9 of this spec's premises as TRUE, listed at the end of its report, including the `_wait_for_code` mechanism (3.1), fan-out delivery, `CODEX_HOME` honouring, the `(home)`-only `spawn_login` fakes, that nothing reads `${CODEX_HOME}/skills`, and that Appendix A's `bobi/cli.py` re-based offsets are correct.

### 9.1 Blockers

**A1 (codex F1). The ask must be posted *after* the listener is connected, not before.**
Section 3's step 2 posts the ask and step 3 then blocks; 3.2(c) factors the listener out of `_wait_for_code`, which registers it.
So as written, the subscription is established after the message a human can already reply to.
This repo's own contract is the opposite, stated twice: "Replay is recovery, not a substitute for establishing the requested live subscription first" (`bobi/events/client.py:207-211`) and "Subscribe before publish, and only publish once the subscription's WS is actually live" (`bobi/inbox.py:444-447`).
A reply lost in that window is **unrecoverable**, not merely late: replay is cursor-based per deployment (`docs/EVENT_SERVER.md:432-441`), and each `register` call mints a fresh deployment id with a fresh session (`event-server/core/src/core.ts:1453`, `:1471`), so a deployment created after the event was published was never an indexed subscriber for it.
The exposed window is from the post until `addSubscription` lands server-side (`event-server/core/src/core.ts:1467-1469`), and `_register_login_channel` plus `register` are remote calls, so it is not instantaneous.

**Fold.** Invert the order in section 3: resolve the channel, register the listener, `wait_connected`, *then* post the ask, capture its `ts`, install the thread predicate, and consume what the queue already holds.
`_wait_for_chat_event` therefore splits into connect and wait phases rather than being one call made after the post.
Verification item 1 and 2 must assert `listener connected -> ask posted -> reply consumed -> spawn_login`, not just reply-before-spawn.

**A2 (codex F2). Q3's "the residual race is cosmetic" is false. Concurrent runs starve the first one.**
The waiter registers under the fixed literal name `"login-bootstrap"` (`bobi/auth_bootstrap.py:543-546`, and again on the `BubbleRejected` retry at `:559-562`).
Registering an existing name in the same bubble deliberately **removes the prior deployment's subscriptions and the deployment itself** (`event-server/core/src/core.ts:1443-1451`, "Supersede any prior deployment with the same name in this bubble").
So with two runs in flight, the second registration de-indexes the first listener.
A human reply to the *first* ask is delivered only to the second listener, whose thread predicate rejects it, and the first command blocks to timeout having posted a live ask nobody can satisfy.
Q3's third reason is the one that fails; its first two stand.

**Fold.** Q3's recommendation is reversed, on refuted evidence rather than on preference:
- Register the listener under a **unique** deployment name (`login-bootstrap-<pid>` or a uuid suffix), so two runs cannot de-index each other. This is the defect's direct cause and is required either way.
- Take a **non-blocking** per-target lock before posting, and have the loser report the existing pending ask rather than posting a second one. A unique name alone still leaves two CLIs racing to write one credential file.
- Verification: a real two-process test, asserting the first run still receives its reply.

Q2's director-only policy remains the primary dedup point; the lock is the mechanism that makes the invariant true when the policy is violated, which is what the earlier "one invariant, two enforcers" objection assumed away.

**A3 (codex F3). The new root `chown` lines are a symlink-dereference escalation path.**
The entrypoint runs as root (`docker/docker-entrypoint.sh:5-8`) and makes the durable volume app-owned on first boot (`:370-373`), so the `bobi` user can create or replace anything under `${DATA_DIR}` between boots.
`chown` without `-h` dereferences, and `mkdir -p` follows an existing symlink silently.
Section 3c's `chown "${APP_USER}:${APP_USER}" "${DATA_DIR}/codex"` therefore transfers ownership of whatever that path points at: a worker that leaves `/data/codex -> /etc` gets `/etc` chowned to `bobi` on the next boot.
Section 6's two `chown`s of `${CODEX_HOME}/auth.json` have the same shape against a single file, and `materialize_codex_api_key_auth`'s root `write_text` (`docker/docker-entrypoint.sh:324-330`) follows an `auth.json` symlink as well.
Main today runs this unsafe directory `chown` only inside the codex-brain branch (`docker/docker-entrypoint.sh:485-488`); section 3c widens it to **every** brain, which is what makes it worth fixing now.

**Fold.** Before any root operation on these paths, reject what is not the expected kind:
- `${DATA_DIR}/codex`: if the path exists and is a symlink or not a directory, log loudly and remove the link itself (`rm -f`, which unlinks rather than following) before `mkdir -p`. Never `chown` it unchecked.
- `${CODEX_HOME}/auth.json`: `[ -L ]` test and refuse, or `chown -h`, before any root write or ownership change. The same guard belongs in front of `materialize_codex_api_key_auth`'s write.
- Verification: add a docker-lane case per planted symlink (a directory link at `/data/codex`, a file link at `auth.json`) asserting the protected target's ownership and content are untouched and the boot fails loudly rather than silently escalating.

### 9.2 Majors

**A4 (codex F4). The mandated `fields.thread_ts` filter makes Discord and WhatsApp permanently un-ready.**
3.2(d) states the thread condition as `fields.thread_ts == <the ask's ts>` "on both paths", where both paths means legacy Slack and gateway.
But the gateway path serves three sources, not one: `_resolve_login_channel` accepts `discord` and `whatsapp` (`bobi/auth_bootstrap.py:307-309`), and `test_run_bootstrap_posts_to_discord_conversation` (`tests/test_auth_bootstrap.py:637-701`) pins Discord as a supported login destination today.
Neither source carries a thread anchor: the Discord adapter's `fields` are `user_id`, `user_name`, `channel_id`, `message_id`, `application_id` with no `thread_ts` (`event-server/core/src/adapters/discord.ts:108-114`), WhatsApp likewise (`event-server/core/src/adapters/whatsapp.ts:79-105`), and both declare `threads: false` (`event-server/core/src/channels.ts:483`, `:644`).
So on either source no inbound event can satisfy the predicate, and the ask's own text ("reply to this message in a thread") is wrong there too.
Section 3.1's claim that the event-bus path "already works on Slack, Discord and WhatsApp" is true of main and false of this design.

**Fold.** The predicate cannot be stated once for all sources. The remedy is a scope choice, posed as **[Q5](#93-new-questions-for-zach)**; whichever Zach picks, 3.1's tri-channel sentence must be qualified and the ask text must stop naming threads on a threadless transport.

**A5 (codex F5). An unvalidated `target` disables both guards while silently resolving to Claude.**
`_active_spec()` falls back to Claude for any unrecognized `BRAIN_ENV` value (`bobi/auth_bootstrap.py:117-120`, `_SPECS.get(kind, _SPECS["claude"])`), and 3.3 gates both refusals on `target is None`.
Two consequences the spec does not state:
- `login-bootstrap typo` disables the gateway guard *and* the shadow-env guard, then runs the **Claude** flow. A typo is a guard bypass.
- `login-bootstrap claude` disables the Claude shadow-env guard for the Claude credential it is about, which is exactly the invariant 3.3 argues the guards exist to protect.

**Fold.** Validate `target` against the known brain kinds and reject an unknown value with a clear error instead of relying on `_active_spec`'s fallback; 3.3's one-line override only becomes safe once the input is closed.
Scope the shadow-env guard by the **resolved** provider rather than by whether an argument was supplied: 3.3's own argument is that `OPENAI_API_KEY` does not shadow a codex credential on disk, which is a statement about codex, not about argument presence.
`target="claude"` must keep both guards.
The gateway-brain case is a policy question, posed as **[Q6](#93-new-questions-for-zach)**.

**A6 (codex F6). The documented 401 recovery is a no-op against the credential state that produces a 401.**
Codex validation only requires a non-blank `tokens.refresh_token` (`bobi/auth_bootstrap.py:162-171`); it never proves the token is live, unrevoked, or bound to the intended account.
A structurally valid file short-circuits both entry points: `run_bootstrap` returns `True` at `bobi/auth_bootstrap.py:629-632`, and the CLI returns even earlier with "Subscription credentials already present" (`bobi/cli.py:827-829`).
So the 3.5(b) recovery path, director runs `login-bootstrap codex` on a worker's 401, does nothing in the revoked, server-invalidated, and wrong-account cases, which are the common causes of a real 401.
Section 6's "codex OAuth has no refresh expiry, so this is once per machine" is correct about the schema (the code comment says the same) and wrong as an availability claim: revocation is not expiry.

**Fold.** Restate that bullet as "the schema carries no refresh-token expiry field", not "no expiry".
A machine whose credential is present but rejected has **no shell-independent recovery** in this design; that is now stated rather than implied.
The remedy needs a surface section 6 currently excludes, so it is posed as **[Q7](#93-new-questions-for-zach)**.

**A7 (codex F7). Q1 covers only silence. It must cover the failures that abort before the timeout.**
Q1's three options all assume the ask was posted and nobody answered. Three earlier failure modes land in the same unguarded `:575` call under `set -euo pipefail` (`:14`), before the supervisor exists (`:590-639`):
- `BOBI_LOGIN_CHANNEL` unset or misconfigured raises immediately (`bobi/auth_bootstrap.py:648-653`): a permanent crash loop with no ask and no health surface.
- `event_server_url` unconfigured or the event server unreachable raises in the waiter (`bobi/auth_bootstrap.py:521-526`), and registration makes remote calls before any wait begins. Under Option C this restarts fast and re-posts into the saved thread on every cycle, which is the Slack spam Option C exists to avoid.
- A Slack post failure means no ask at all, and boot loops silently.

**Fold, without deciding Q1.** Two additions to Q1 as posed:
- A failure-state table covering post failure, registration failure, connection loss, timeout, and restart, for each of A, B and C. The options stay Zach's choice; the table is what makes them comparable.
- Option C's `${DATA_DIR}/codex/.login-ask-<target>` is durable read-modify-write state and must use `atomic_write_text` plus `file_lock` (`bobi/fsutil.py:1-17`, `:161-172`, "only the lock keeps a concurrent updater's change from being overwritten"). Q1 as written specifies neither, and this repo's state module exists specifically to stop a seventh hand-rolled writer.
- The ask text must name the agent, instance and target. Two machines in one fleet currently post indistinguishable asks, and under Option C they would share a thread.

**A8 (codex F8). The widened sweep ships before the schema it depends on is observed.**
Section 3.4 widens the subscription sweep from "delete recognized API-key auth" (`docker/docker-entrypoint.sh:553-562`) to "delete anything `credential-status` does not accept", and the accepted shape is only `tokens.refresh_token` (`bobi/auth_bootstrap.py:162-171`).
Appendix A's "Could not verify" requires codex's `--device-auth` write shape to be re-confirmed "before this ships", but verification item 15 is filed under "Live, post-merge" and §8 step 7 schedules it **post-roll**.
Those two cannot both hold.
If the real device-auth file is compatible-looking but differently shaped, the first restart after a successful login deletes the credential that login just minted, on every machine.

**Fold.** The contradiction is resolved in Q1's favour of safety, not by reordering a verification item on paper: either the fixture is obtained before merge, or the sweep stays conservative.
Which one is **[Q8](#93-new-questions-for-zach)**.
Independent of that answer, the sweep must not unconditionally delete an unclassifiable file: quarantine it with a recoverable rename and a loud log line.
A destructive default on an unknown schema is the wrong failure direction for a credential the human minted by hand.

**A9 (codex F9). Timeout warns, does not reap, and loses a late reply.**
On device-flow timeout the code logs a warning and falls through (`bobi/auth_bootstrap.py:701-705`); the `finally` block calls `proc.terminate()` with no `wait`, no `kill` escalation, and no process-group signal (`:706-712`), while the child was spawned `start_new_session=True` (`:220-224`) and so is its own session leader.
A login CLI that ignores SIGTERM survives as an orphan holding the pty.
Separately, the single 600s budget spans ask-wait plus URL scrape plus device authorization, so a human who authorizes at minute eleven loses even though the device code is valid for fifteen; and a late reply cannot rescue the run, because the next fixed-name registration removed that deployment (A2).

**Fold.** Give the ask wait, the URL scrape, the device authorization and the post-code exit **separate** budgets rather than one shared timeout, so a slow human does not consume the code's lifetime.
On timeout, signal the process group, `wait` with a bound, escalate to `kill`, and reap.
Post an explicit expired or cancelled outcome into the thread saying a fresh reply is required, so the thread does not end on "waiting for you to authorize".
Verification: a fake that ignores `terminate`, and a reply arriving after the timeout.

**A10 (codex F10). Verification items that pass a broken implementation.**
Mostly derivative of A1 to A9, with two findings of its own:
- **Item 12 passes if section 3c is omitted entirely.** It asserts only that each boot exits 0, and the spec says so outright ("there is nothing else to assert, because 3c creates no object whose kind can be wrong"). That is not a regression pin. It must assert the exported `CODEX_HOME` value, that `${DATA_DIR}/codex` is a real directory and bobi-owned, and that both hold across a brain switch.
- **No item asserts the outcome post.** Section 3's step 6 posts the outcome into the thread; item 5 pins only the device code.

**Fold.** Add items for: subscribe-before-post ordering (A1), two concurrent runs (A2), planted symlinks (A3), a non-Slack destination under whatever Q5 decides (A4), an invalid and a `claude` target (A5), a revoked-but-structurally-valid credential (A6), channel and event-server outage plus stored-ask corruption (A7), the quarantine path (A8), process reaping and a post-timeout reply (A9), and the outcome post.
Item 12 is rewritten as above.

Codex **retracts** the task brief's suspicion that the shell and docker lanes are fictional: items 11, 13 and 14 are implementable with the existing bind-volume and container harness. The defect is assertion strength, not the lane.

### 9.3 New questions for Zach

These four are not folded, because each one changes what this spec is for rather than fixing how it works.
Sections 1 to 8 stand as written until they are answered.

**Q5. Is ask-first login Slack-only in v1 (A4)?**
- **Option A:** declare it Slack-only, refuse a `discord` or `whatsapp` `BOBI_LOGIN_CHANNEL` with a clear error, and qualify 3.1. Z1 names Slack specifically, so this gives up nothing Z1 asked for. It does remove a path main supports today and `tests/test_auth_bootstrap.py:637-701` pins, so that test changes meaning.
- **Option B:** make correlation source-specific. Discord needs its adapter to expose the referenced-message id; WhatsApp needs a different rule entirely. This keeps three transports and adds a per-source branch to the one filter, against the standing "no cruft" bar.
- No recommendation. Option A is smaller and matches Z1; Option B is the only one that keeps a supported path working.

**Q6. May a gateway-brained team mint a direct provider credential for a CLI tool (A5)?**
Scoping the gateway guard to `target is None` means `login-bootstrap codex` on a Claude-gateway team runs `codex login --device-auth` straight at OpenAI, outside the gateway that team authenticates through.
The superseded plan treated that as bypassing the gateway's audit and spend boundary.
3.3's argument (the guard is about the *brain's* credential) is sound for the brain and silent on this.
This is a policy call about where provider spend is allowed to originate, not a technical constraint, so it is posed rather than answered.
Verification item 8 currently asserts the bypass as intended behaviour, so it moves with the answer.

**Q7. Does a rebind path belong in v1 (A6)?**
Without one, a machine holding a revoked or wrong-account codex credential has no recovery that does not require `fly ssh`, which Z1 says cannot be assumed.
Section 6 currently excludes `--rebind` and new flags as cruft, and that exclusion predates A6.
The shape, if yes: after the human's ready reply, quarantine the existing credential and start a fresh login, gated on the same human reply that already gates minting, so it adds no new trust surface.
Posing it because it reverses a stated scope exclusion.

**Q8. Fixture before merge, or keep the sweep conservative (A8)?**
- **Option A:** obtain and sanitize a real `--device-auth` `auth.json` before merge, which makes the widened sweep safe but blocks the PR on one human login.
- **Option B:** keep the sweep deleting only recognized API-key auth, as main does, and handle malformed credentials only through Q7's explicit rebind. Unblocks the PR and drops a capability the spec claims.
- No recommendation; the cost is a merge delay against a destructive default on unobserved data.

### 9.4 Retracted, with evidence

**Codex F11's first claim, that this spec's `bobi/cli.py` citations are stale against current main: declined.**
Appendix A already states the drift and lists the corrected mapping: `:713 -> :807`, `:714-715 -> :808-809`, `:733 -> :827`, `:737 -> :831`, `:471-482 -> :514-525`, `:1129`/`:1195` -> `:1221`/`:1265`.
Codex's "current CLI locations" list reproduces those same seven offsets exactly, so it confirms Appendix A rather than correcting it, and codex says as much ("The CLI drift is only an offset and does not invalidate the arguments").
The body keeps the `83bebe49` numbers deliberately, with Appendix A as the single re-basing record; that is a documented convention, not an error.

Codex F11's other two claims **are** folded, as corrections to Appendix A:
- `bobi/tool_library/gstack/guide.md:7-8` ("with its skills linked under `~/.claude/skills/` (and `~/.codex/skills/`)") is a **fourth** in-repo `~/.codex/skills` reference. The round-3 Appendix row claiming "the only `~/.codex/skills` references in the tree are gstack's installer (`tool_library/gstack/tool.yaml:33`, `:35`, `:51`)" is stale: the round-4 row and §6's scope list both already name four. The round-3 row is wrong and the round-4 row is right.
- An entrypoint path **does** produce a root-owned `auth.json`. `materialize_codex_api_key_auth` writes the file as root (`docker/docker-entrypoint.sh:324-330`) and only chowns afterwards (`:333`), so a death between the two leaves it root-owned. Section 6's "no sub-object that stays root-owned" is about `codex` invocations and should be qualified to say so; the write-then-chown window is a state the two new `chown` lines repair, which strengthens the case for them rather than weakening it.
