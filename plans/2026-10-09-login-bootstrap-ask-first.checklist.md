# Verification checklist - #958 ask-first `login-bootstrap`

Derived verbatim from [the spec's section 6](2026-10-09-login-bootstrap-ask-first.md#6-verification-plan).
One row per verification item. Committed before implementation, ticked with evidence as each lands.

Status vocabulary: `[ ]` not started, `[~]` test written and failing, `[x]` implemented and green.

## Unit lane - `tests/test_auth_bootstrap.py`

Baseline on untouched main: see "Suite comparison" below.

| # | Item | Status | Test | Evidence |
|---|---|---|---|---|
| 1 | Ordering both flows: connect -> post -> reply -> spawn | [ ] | | |
| 2 | A bot reply (`fields.bot_id`) is not ready; a following human one is | [ ] | | |
| 3 | Correlation, channel destination: 4 legs, `thread_ts` only, never event type | [ ] | | |
| 4 | Correlation, DM destination: no `thread_ts` is still ready (Slack/Discord/WhatsApp) | [ ] | | |
| 5 | Discord guild channel refused at `_resolve_login_channel`; Discord DM resolves | [ ] | | |
| 6 | Discord inbound requirement now covers `device_poll` | [ ] | | |
| 7 | URL + device code posted into the ask's thread, both post paths | [ ] | | |
| 8 | Outcome post lands in the thread: success, failure, timeout | [ ] | | |
| 9 | `target` retargets command and credential path | [ ] | | |
| 10 | `spawn_login` still called with exactly one argument | [ ] | | |
| 11 | Target validation by named case: `stub` rejected, `gateway-openai` -> codex, unknown rejected, `claude` accepted | [ ] | | |
| 12 | Guard scoping by resolved provider, 5 legs | [ ] | | |
| 13 | D6 limitation asserted as documented behaviour | [ ] | | |
| 14 | `cli.py` pre-check is target-aware | [ ] | | |
| 15 | Re-attach posts nothing, sees a gap reply; empty `user` not human; non-Slack skips | [ ] | | |
| 16 | In-flight marker + id recovery; failed post leaves state byte-identical; cleared only on success | [ ] | | |
| 16a | A destination change invalidates stored state | [ ] | | |
| 16b | Consumed reply not re-read as the pasted code | [ ] | | |
| 16c | A pre-threaded destination still correlates | [ ] | | |
| 17 | Phase budgets do not share | [ ] | | |
| 18 | Reaping: process group, bounded wait, kill escalation | [ ] | | |
| 19 | Rebind: short-circuit, ask, collision-free quarantine, symlink refused | [ ] | | |
| 20 | Two concurrent runs do not de-index each other | [ ] | | |
| 21 | Unchanged: `test_login_bootstrap_posts_only_to_the_configured_channel` still passes | [ ] | | |

## Shell lane - runnable without docker

| # | Item | Status | Test | Evidence |
|---|---|---|---|---|
| 22 | Boot-order matrix with real assertions, `${HOME}` persisted and recreated | [ ] | | |
| 23 | Planted symlinks: non-zero exit, diagnostic, protected target untouched | [ ] | | |
| 24 | Credential guard + conservative sweep: 5 states x 2 auth modes x 2 brain kinds | [ ] | | |
| 24a | Catch-up read against a real event server and real channel adapter | [ ] | | |
| 24b | Non-Slack skip asserted against the real adapter set (WhatsApp) | [ ] | | |
| 25 | The export reaches the manager through `gosu ... env` | [ ] | | |

## Docker lane - `-m docker`, needs a built image

| # | Item | Status | Test | Evidence |
|---|---|---|---|---|
| 26 | Claude brain + subscription: `/data/codex` bobi-owned, `CODEX_HOME` in manager env, survives restart | [ ] | | |
| 27 | Codex brain boots clean (regression pin for the 3c abort) | [ ] | | |

## Live, post-merge - owed on the issue as proof of work

| # | Item | Status | Evidence |
|---|---|---|---|
| 28 | One real login end to end, then `codex exec -s read-only` returning 0 | [ ] | post-roll |
| 29 | Survives a restart and re-attaches silently; mid-wait restart posts no second ask | [ ] | post-roll |

## Suite comparison

| Run | Command | Result |
|---|---|---|
| Baseline, untouched main `8eef3fe4` | `pytest tests/ --ignore=tests/integration/ --ignore=tests/e2e/ --timeout=30 -q` | pending |
| This branch | same | pending |

## Mutants

Every new test is proven by a named mutant: the change is applied, the test is run and
observed to fail, and the tree is restored. One row per mutant.

| Mutant | Change | Test that must fail | Observed |
|---|---|---|---|
| pending | | | |
