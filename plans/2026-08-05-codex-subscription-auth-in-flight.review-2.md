Read the spec, the code it modifies (`auth_bootstrap.py`, `cli.py`, `gateway.py`, `doctor.py`), the existing codex tool guide, and `docs/design-system/` (which this repo makes the source of truth for anything on a Bobi surface).

# Design review: 2026-08-05-codex-subscription-auth-in-flight

Headline: the engineering is careful, the four human-facing surfaces are not. Every one of them was designed by describing behavior rather than by writing the artifact. The spec contains one message draft, no `--help` text, no `--status` output, and no success message.

---

## 1. The Slack message - 4/10

**What's wrong**

- **It violates the repo's own design system.** `docs/design-system/README.md`, Content Fundamentals: "**No emoji, ever** - the two glyphs the brand permits in copy are `↳` and `☾`, plus the figure glyphs `◇ ▢ ▤ ◆ ✓`." The draft opens with 🔐, and §"Notification and provenance" explicitly rewrites the `"✅ … starting up"` / `"❌ … Fallback"` strings, so those are in scope and re-ship the violation. The house glyph set already exists in code: `bobi/validate.py::status_glyph` (`✓/✗/⚠`, with `[OK]/[ERROR]/[WARN]` fallback), used by `doctor`.
- **The headline states the cause, not the ask.** "hit a codex 401" is bobi's internal event. The house model for this exact object is `GateApproval`, whose contract is `title` = "What is being approved, in plain words" and `detail` = "Why this needs a person," and whose adherence rule is "always name the workflow and step so the decision is auditable." The draft names the run id (an opaque token) and never names the workflow, the step, or what is blocked. The operator cannot judge whether to get out of bed.
- **The machine id is the least actionable token in the message and it is on line 2.** It is only useful attached to the verify command, which is where it belongs.
- **The deadline is absolute UTC, to the second.** `Polling until 00:20:14 UTC` forces a delta computation at 2am in an unknown timezone, at a precision that scrape and post jitter make fake ("Numbers are real or absent"). `GateApproval` uses a duration (`expiresIn: "14m"`) precisely because it "reinforces that nothing proceeds."
- **`--status` is sold as the security control and rendered as a footnote.** §"Notification and provenance" makes it the whole answer to lookalike-code phishing, then ships it in trailing italics after the code, in the position of a tip. A control nobody runs is not a control, and Q3's mitigation list is unsupported as long as it renders there. It must be an imperative step *above* the code, or the phishing argument should be dropped.
- **Three messages is the wrong count, and "supersedes" is not a thing Slack does.** The cancellation message does not supersede the code post, it appends below it; the live-looking dead code stays in scrollback forever. The plumbing for the right answer already exists and the spec does not use it: `bobi/events/gateway.py:61` `channels_send(..., mode="update", edit_ref=...)` and it returns `ts`. `_post_login_message` currently discards that return value; capturing it is a one-line change.

**What a 10 looks like:** one message, edited in place through pending → authorized → expired. No emoji. Headline is the ask. Three lines the operator can act on. Provenance folded into a verify line they can actually run. Duration deadline. The final state records who authorized and which account got bound.

**The edit.** Post at step 4 without the numbered block, edit the same `ts` at step 7 to add it:

```
◆ *codex login needed* - @on-call
`eng-team` cannot run its cross-model review until codex is authorized.
Stopped: `issue-lifecycle.yaml` · step `review` · run `wf-issue-lifecycle-4f21`.

Confirm bobi asked for this before you enter anything:
`fly ssh console -a bobi-eng-team -s d8d0926a026d28 -C "bobi agent eng-team auth codex --status"`
It prints the same code from the machine's own lock file. A code that is not
there did not come from bobi.

1. https://auth.openai.com/codex/device
2. enter `9S1A-79NNG`

Expires in 5m. Ignoring this costs nothing: the review runs single-model.
```

Edit to, on success:

```
✓ *codex login complete* - authorized 00:16 UTC, bound to `luke@modalabs.ai`.
`eng-team` on `d8d0926a026d28`. The review pass resumed.
Rebind to a different account: `bobi agent eng-team auth codex --rebind`.
```

Edit to, on timeout (replaces the code, so no live-looking code survives):

```
✗ *codex login expired* - nobody authorized within 5m. That code is dead.
`eng-team` ran the review single-model and recorded the cross-model opinion as owed.
Retry any time: `bobi agent eng-team auth codex`.
```

Two honesty notes to write into the spec rather than leave implicit: the `legacy_slack_channel` branch of `_post_login_message` calls `post_message(token, dest, text)` and has no edit path, so it degrades to append there. And "bound to `<account>`" requires that codex actually exposes the account (`login status` output or the `id_token` claim in `auth.json.tokens`); Appendix A only verified the api-key file shape. If it does not expose it, the spec must say so - silently omitting it leaves the operator unable to tell whose plan is being spent.

Also flag for the design-system owner: `✗` is in `status_glyph` but is not in the README's permitted glyph list. One-line amendment or use `◇`.

---

## 2. The agent-facing contract - 4/10

**What's wrong**

- **The triple timeout is a trap, and the spec half-diagnosed it.** §C correctly killed the earlier draft's "Bash timeout expressed as a shell comment, which sets nothing"; this draft makes the *shell* `timeout` real but leaves the *Bash tool* parameter as prose inside a comment, which has the identical failure mode. The harness never reads comments. An agent under load copies the block, misses it, and the tool kills at 120s mid-ceremony: orphaned poller, burned device code, operator walking to their laptop toward a code nobody will collect. Step 5's reaper fixes that on the *next* run, which is not a defense for this operator.
- **Three numbers, three places, two units** (300 / 420 / 480000). And 420 against a 300s budget means a hung command sits two minutes past its own deadline for nothing.
- **The two exit tables disagree on ordering.** The wire-shape table is `0, 2, 3, 1`; the recipe's `case` is `0, 3, 2, *`, ninety lines apart in one document.
- **`codex exec -s read-only "$PROMPT" < /dev/null` appears three times**, and it does not match the invocation the guide it sits beside actually teaches (`bobi/tool_library/codex/guide.md` uses `-c 'model_reasoning_effort="high"'`). The recovery path silently runs a weaker review than the happy path.
- **`--force` is unexplained ceremony in a security-adjacent command.** §D already declared the presence pre-check non-authoritative, so `--force`'s only job is to skip a check that does not decide anything. An agent that reasons about a flag named "force" may drop it, breaking exactly regression test 11.
- **`echo` as the degradation action is too weak.** "The cross-model opinion is owed" is a claim that must survive into the review artifact, not stdout.

**What a 10 looks like:** one bolded precondition, one function defined once, one command with zero flags and zero visible timeouts, four cases whose bodies each name the agent's next action in the agent's own terms.

**The edit.** Replace §"The recovery contract agents follow" with:

````markdown
### codex returns 401

This container has no codex credential yet. One command asks the operator to
authorize it over Slack and blocks until they do, up to 5 minutes. One login
authorizes every worker in this container, including ones already running.

**Set the Bash tool's `timeout` parameter to `420000` for this call.** Its
default is 120s and would kill the login while the operator is still typing.

```bash
review() { codex exec -s read-only -c 'model_reasoning_effort="high"' "$1" < /dev/null; }

review "$PROMPT" || {
  bobi agent "$BOBI_AGENT_NAME" auth codex
  case $? in
    0) review "$PROMPT" ;;                  # authorized: the pass runs
    2) OWED="operator did not authorize within the login window" ;;
    3) review "$PROMPT" || OWED="another worker is running the login, or one just finished" ;;
    *) OWED="codex login is unavailable on this machine (see the error above)" ;;
  esac
}
# If $OWED is set: run the pass single-model, and record that line verbatim in
# your review output under "cross-model opinion owed:". Do not re-run auth codex.
```
````

Then make the command own its budget so `timeout 420` is unnecessary (§C already claims `--timeout` bounds the whole command - the wrapper exists only because the spec does not trust its own §C), and drop `--force` entirely.

Separately: exit 3 means "in flight **or** cooling down", but the recipe's message asserts "already in flight" and is wrong half the time. Either split the codes or have the command print the distinguishing reason on stderr. Four codes is already the most an agent will reliably hold, so print the reason.

---

## 3. The CLI surface - 3/10

**What's wrong**

- **The name pairs badly with its own sibling.** `login-bootstrap` and `tool-login` share a word in opposite orders and differ on two orthogonal axes at once (brain vs tool, boot vs runtime). Nothing in the pair tells you which is which. Worse, `tool-` encodes an internal taxonomy the caller does not have: an agent hitting a 401 thinks "codex is not authed", not "codex is a tool target".
- **It is also not a login.** The caller does not log in; it asks a human to. The design system's Bobi verb list is land, triage, dispatch, run, work, hand off, report, consolidate, wake, approve, halt. "login" is not in it.
- **The library layer is already unified and the CLI is not.** §A gives you `BRAIN_TARGET = "brain"` and `resolve_spec(target)`. The CLI should mirror that: **`bobi agent <name> auth [<tool>] [--status]`**, with `login-bootstrap` kept as a hidden alias (a released image pins the old name in `docker-entrypoint.sh:566`, so alias rather than break). Cost: one registration plus an alias.
- **`--status` is a command wearing a flag's clothes.** It never posts, never spawns, never blocks - it suppresses the entire behavior of the command it is attached to. This repo's convention is that such things are commands (`status`, `doctor`).
- **`doctor` already owns this question and the spec does not notice.** `bobi/doctor.py` ships a `Claude auth: authenticated` check rendered with `status_glyph`. An operator at 2am types `doctor`; they do not type a flag on a command they have never heard of. Add a codex row to `run_doctor()` that reports the last probe result and its age from the same state file `--status` reads (cheap, no network in a health check), and keep `--status` for the in-flight/lock/cooldown detail. One front door, one detail view - the same "one mechanism, both jobs" instinct §D applies one layer up.
- **No `--help` text anywhere in the spec.** For a command whose entire purpose is a human ceremony at 2am, the docstring *is* the manual, and `login-bootstrap`'s existing docstring is the in-repo model.

**What a 10 looks like:** a name an operator guesses, a flag set with nothing dead in it, and a docstring that answers "what will this do to my night" before they run it.

**The edit.** Ship this docstring:

```
bobi agent eng-team auth codex --help

Ask the operator to authorize a CLI tool over chat, and wait.

    codex authenticates with an OAuth subscription, not an API key. When it
    401s inside a running container, this posts a device-login request to
    $BOBI_LOGIN_CHANNEL and blocks until a human authorizes it or the window
    closes. One login authorizes every worker in the container, including
    ones already running, and survives a restart.

    The outcome is decided by a real call to the tool, never by a file on
    disk: a credential can be present and still be dead.

    Exit 0 authorized · 2 nobody authorized in time · 3 another worker is
    already running it · 1 unusable (no login channel, unknown tool, gateway
    team, or the tool itself is erroring).

Usage:
    bobi agent eng-team auth codex               # ask, and wait up to 5m
    bobi agent eng-team auth codex --status      # what is true now; asks nobody
    bobi agent eng-team auth codex --rebind      # drop the credential, ask again
    bobi agent eng-team auth --status            # same, for the team's brain

Options:
  --timeout SECONDS  How long to hold the run open while the operator walks to
                     their laptop. Whole-command budget, default 300. At the
                     deadline the pending code is cancelled in chat, exit 2.
  --status           Print local auth state and exit. Never posts, never waits.
  --rebind           Remove the stored credential and run the ceremony. Use when
                     the wrong account was bound, or to rotate.
```

Also specify `--status`'s actual output. It is currently four nouns in a table cell.

---

## 4. The operator journey - 3/10

**Dead ends**

1. **The verify command cannot be run from where the operator is.** They are in Slack, likely on a phone; `bobi agent … --status` requires being on that Fly machine, and the message gives no `fly ssh console` line and no app name. The design's central anti-phishing control has an unstated prerequisite that defeats it at exactly the hour it is needed. The ids are already in hand (`$FLY_APP_NAME`, `$FLY_MACHINE_ID`), so this is a cheap fix - see the message in §1.
2. **There is no rotation or unbind path, and §D's own ordering forecloses one.** Ordering step 2 is "probe passes → exit 0 without a ceremony". So a credential that works but is bound to the *wrong account* can never be replaced through this command, `--force` included (`--force` only skips the presence check). Every credential surface owes an answer to "someone left the team" and this one has none. Add `--rebind`: remove `${DATA_DIR}/codex/auth.json`, then run the ceremony.
3. **Which account got bound is never surfaced.** Two people can authorize on different runs and nobody can tell whose ChatGPT plan is paying. First question a security-conscious operator asks after typing a code into an OpenAI page.
4. **The precondition carrying most of the security weight is unstated.** §"Notification and provenance" correctly reasons that text provenance is forgeable *in a channel an attacker can post in*. The real control is therefore channel hygiene, and the spec never states it: **`$BOBI_LOGIN_CHANNEL` must be a channel only bobi can post to.** Writing that down converts Q3 from an open worry into a stated deployment constraint, and it is stronger than `--status` because it works from a phone.
5. **Nothing answers "is a login pending on a machine nobody pinged me about."** Out of scope removes the `requires:` preflight, so there is no fleet signal; if `$BOBI_LOGIN_CHANNEL` is unset or the send fails, the ceremony exits 1 and the operator is never told that machine cannot review. Minimum: the cooldown stamp already exists, so have `--status` and the doctor row report `codex: unauthenticated since <t>, last asked <t>` and state plainly that fleet-wide visibility is out of scope and the next 401 is what surfaces it.
6. **No way to say "not now."** Ignoring is the only option and it silently burns 300s of a worker's budget. Cheapest honest fix is one clause in the message: "Ignoring this costs nothing: the review runs single-model."
7. **The success message is unspecified.** "post the outcome" plus a note that the existing `"✅ … starting up"` is wrong. Specify it: who authorized, which account, that the run resumed, how to undo.

**What a 10 looks like:** every question the operator has at each step is answered in the surface they are already looking at, and the two irreversible-feeling actions (authorize, rebind) each have a stated undo.

---

## Top 3 changes

1. **One live message, not three.** Use `channels_send(mode="update", edit_ref=ts)` - already in `bobi/events/gateway.py:61`, and `_post_login_message` merely discards the `ts` it returns. Pending → authorized/expired edits in place. This deletes the dead-code-in-scrollback problem outright instead of mitigating it with a third post, satisfies the house gate contract (one object, live state, names the workflow and step), and dissolves the "is three messages spam" question. Drop the emoji while you are in those strings.

2. **Strip the numbers and flags off the agent surface.** Delete `--force` (§D already made presence non-authoritative), delete `timeout 420` (the command owns its budget per §C), and promote the Bash-tool timeout from a comment to a bolded precondition line above the block - a comment sets nothing, which is the failure §C already caught once. Define `review()` once so the invocation cannot drift, and keep `-c 'model_reasoning_effort="high"'` so the recovery path matches the guide it lives beside. Fix the two exit tables to the same order.

3. **Close the operator's two dead ends.** Make the verify line a runnable `fly ssh console -a <app> -s <machine> -C "…"`, and add `--rebind`, because §D's ordering currently makes a working-but-wrong credential permanent. Name the bound account in the success state, or state explicitly that codex does not expose it.

**Free fixes while in there:** rename to `bobi agent <name> auth [<tool>]` with `login-bootstrap` as a hidden alias, so the CLI mirrors the `resolve_spec(target)` layer you are already building; make `--status` real output rather than four nouns in a table cell; add a codex row to `run_doctor()` reading the same state file, so the 2am question has one obvious front door; and write the `--help` text into the spec, since for this feature it is the primary UI.
