# Verification checklist - #958 ask-first `login-bootstrap`

Derived verbatim from [the spec's section 6](2026-10-09-login-bootstrap-ask-first.md#6-verification-plan).
One row per verification item, ticked with the test that proves it.

`T:` = `tests/test_auth_bootstrap.py` · `E:` = `tests/test_entrypoint_codex_home.py`
`G:` = `tests/integration/test_channel_gateway.py` · `C:` = `tests/integration/test_container_image.py`
`L:` = `tests/test_tool_library.py` · `I:` = `tests/test_cli.py`

## Unit lane

| # | Item | Status | Test | Mutant |
|---|---|---|---|---|
| 1 | Ordering both flows: connect -> post -> reply -> spawn | [x] | `T:test_ask_first_orders_connect_post_reply_spawn_{paste_back,device_poll}` | M1 |
| 2 | A bot reply is not ready; a following human one is | [x] | `T:test_a_bot_reply_alone_never_satisfies_the_wait`, `T:test_a_human_reply_after_a_bot_one_is_the_one_consumed` | M2 |
| 3 | Correlation, channel: 4 legs, `thread_ts` only, never event type | [x] | `T:test_channel_correlation_keys_on_thread_ts_only` (4 params) | M3 |
| 4 | Correlation, DM: no `thread_ts` is still ready | [x] | `T:test_dm_correlation_accepts_a_reply_with_no_thread_anchor` (slack/discord/whatsapp) | M4 |
| 5 | Discord guild channel refused; Discord DM resolves | [x] | `T:test_discord_guild_channel_is_refused_as_a_login_destination`, `T:test_discord_dm_still_resolves` | M6 |
| 6 | Discord inbound requirement now covers `device_poll` | [x] | `T:test_discord_inbound_requirement_covers_device_poll` | M7 |
| 7 | URL + device code posted into the ask's thread, both paths | [x] | `T:test_device_code_is_posted_into_the_ask_thread_{legacy_slack,gateway}`, `T:test_gateway_posts_with_no_inbound_ref_are_still_thread_anchored` | M8 |
| 8 | Outcome post in the thread: success, failure, timeout | [x] | `T:test_outcome_post_lands_in_the_ask_thread` (2 params), `T:test_timeout_outcome_post_lands_in_the_ask_thread` | M8 |
| 9 | `target` retargets command and credential path | [x] | `T:test_target_retargets_command_and_credential_path` | M9 |
| 10 | `spawn_login` still called with exactly one argument | [x] | `T:test_spawn_login_is_still_called_with_exactly_one_argument` | - |
| 11 | Target validation by named case (6 params incl. `stub`) | [x] | `T:test_target_validation_by_named_case` | M9 |
| 12 | Guard scoping by resolved provider, 6 legs | [x] | `T:test_guards_are_scoped_by_resolved_provider`, `T:test_codex_brain_refusal_with_openai_key_is_still_pinned` | M10 |
| 13 | D6's limitation asserted as documented behaviour | [x] | `T:test_gateway_brained_team_may_mint_a_direct_codex_credential` | M10 |
| 14 | `cli.py` pre-check is target-aware | [x] | `T:test_cli_precheck_is_target_aware` | M11 |
| 15 | Re-attach posts nothing, sees a gap reply; empty `user` not human; non-Slack skips | [x] | `T:test_reattach_posts_nothing_and_consumes_a_gap_reply`, `T:test_reattach_blocks_when_the_catch_up_read_finds_nothing` (2), `T:test_catch_up_read_is_skipped_entirely_on_a_non_slack_destination` | M12, M23 |
| 16 | Marker + id recovery; failed post leaves state byte-identical; cleared only on success | [x] | `T:test_marker_with_no_id_recovers_it_from_channel_history`, `T:test_marker_with_no_id_on_a_non_slack_destination_posts_nothing`, `T:test_a_failed_post_leaves_the_ask_state_byte_identical`, `T:test_ask_state_is_cleared_on_success_and_only_on_success` | M13 |
| 16a | A destination change invalidates stored state | [x] | `T:test_a_destination_change_invalidates_the_stored_ask` | - |
| 16b | Consumed reply not re-read as the pasted code | [x] | `T:test_the_consumed_reply_is_not_re_read_as_the_pasted_code` | M14 |
| 16c | A pre-threaded destination still correlates | [x] | `T:test_pre_threaded_destination_correlates_on_the_destination_thread` | M5 |
| 17 | Phase budgets do not share | [x] | `T:test_phase_budgets_do_not_share` | M15 |
| 18 | Reaping: process group, bounded wait, kill escalation | [x] | `T:test_a_login_that_ignores_terminate_is_killed_by_process_group` | M16 |
| 19 | Rebind: short-circuit, ask, collision-free quarantine, symlink refused | [x] | `T:test_rebind_quarantines_collision_free_and_refuses_a_symlink`, `T:test_rebind_refuses_a_symlinked_credential` | M18 |
| 20 | Two concurrent runs do not de-index each other | [x] | `T:test_two_concurrent_listeners_get_unique_deployment_names` | M17 |
| 21 | Unchanged: `test_login_bootstrap_posts_only_to_the_configured_channel` | [x] | passes unchanged; its fakes take the new signatures only | - |

## Shell lane - runnable without docker

| # | Item | Status | Test | Mutant |
|---|---|---|---|---|
| 22 | Boot-order matrix, `${HOME}` persisted and recreated | [x] | `E:test_boot_order_matrix_exports_codex_home` (32 params: 4 brain sequences x baked-skills x fresh-home) | M19 |
| 23 | Planted symlinks: non-zero exit, diagnostic, target untouched | [x] | `E:test_planted_symlinks_abort_the_boot_and_leave_the_target_untouched` (2 params) | M20 |
| 24 | Credential guard + sweep: 5 states x 2 auth modes x 2 brain kinds | [x] | `E:test_credential_guard_and_conservative_sweep` (20 params) | M21, M22 |
| 24a | Catch-up read against a real event server and real adapter | [x] | `G:test_catch_up_read_finds_a_gap_reply_through_the_real_adapter`, `G:test_catch_up_read_treats_the_bots_own_last_message_as_the_baseline` | M12 |
| 24b | Non-Slack skip against the real adapter set | [x] | `G:test_whatsapp_history_is_rejected_by_the_real_adapter_set`, `G:test_the_non_slack_skip_is_taken_without_calling_history` | M23 |
| 25 | The export reaches the manager through `gosu ... env` | [x] | `E:test_the_export_survives_gosu_env_without_dash_i` | M19 |
| 25a | `tool.yaml` honours `CODEX_HOME` and does not clobber OAuth | [x] | `L:test_codex_requires_reads_codex_home_not_the_image_home`, `L:test_codex_requires_subscription_does_not_overwrite_oauth_auth` | M24, M25 |

## Docker lane - `-m docker`

Written and collected; **not run locally** (no docker CLI on the build box).
`container.yml` path-triggers on `docker/**` and `bobi/tool_library/**`, so both
run on this PR.

| # | Item | Status | Test |
|---|---|---|---|
| 26 | Claude brain: `/data/codex` bobi-owned, `CODEX_HOME` in the manager env, survives restart | [x] in CI | `C:test_codex_home_is_on_the_volume_for_a_claude_brain` |
| 27 | Codex brain boots clean (regression pin for the 3c abort) | [x] in CI | `C:test_codex_brain_still_boots_clean_with_section_3c` |

## Live, post-merge - owed on the issue

| # | Item | Status |
|---|---|---|
| 28 | One real login end to end, then `codex exec -s read-only` returning 0 | [ ] owed post-roll |
| 29 | Survives a restart and re-attaches silently; mid-wait restart posts no second ask | [ ] owed post-roll |

## Suite comparison

`pytest tests/ --ignore=tests/integration/ --ignore=tests/e2e/ --timeout=30 -q`

| Run | Result |
|---|---|
| Baseline, untouched main `8eef3fe4` | **7 failed, 5531 passed**, 11 skipped, 236s |
| This branch | **7 failed, 5638 passed**, 11 skipped, 282s |

Failure sets are byte-identical: **zero new failures, +107 tests**. All 7 are
pre-existing environmental failures on this box. Two of them
(`test_homebrew_smoke_script_passes_shellcheck`,
`test_team_packaging_versioned`) pass once `shellcheck` is on `PATH`; the
`shellcheck-py` wheel supplies it, and `docker/docker-entrypoint.sh` is
shellcheck 0.11.0 clean.

## Mutants

Every new behaviour is proven by a named mutant: the change is applied, the
test that must catch it is run and observed to **fail**, and the tree is
restored and verified byte-identical. **33/33 caught.**

The harness also gained a SIGTERM handler: a kill mid-run left one mutation on
disk, and only a post-hoc content check over the fixed lines found it. A
restore that depends on a `finally` is not enough when the process can be
signalled.

The harness requires a real `N failed` in the summary, not merely a non-zero
exit: an empty `-k` selector also exits non-zero (rc 5, "N deselected"), which
scored one mutant as caught by a test that no longer existed.

| # | Mutant | Caught by |
|---|---|---|
| M1 | Post the ask before connecting the listener | item 1 |
| M2 | Accept bot-authored replies | item 2 |
| M3 | Add an event-type filter to the anchor rule | item 3 leg 4 |
| M4 | Require the thread anchor on DMs too | item 4 |
| M5 | Anchor on the ask's `ts` instead of the destination's thread | item 16c |
| M6 | Allow a Discord guild channel as a destination | item 5 |
| M7 | Restore the `paste_back`-only Discord gate | item 6 |
| M8 | Drop `:thread:` from gateway posts | items 7, 8 |
| M9 | Validate the target against the brain registry | items 9, 11 |
| M10 | Key both guards on argument presence | items 12, 13 |
| M11 | CLI pre-check reads the brain's credential | item 14 |
| M12 | Drop the bot baseline from the history read | items 15, 24a |
| M13 | Clear the ask state unconditionally | item 16 |
| M14 | Drop the consumed-reply watermark | item 16b |
| M15 | Share the paste-back timeout with the device wait | item 17 |
| M16 | SIGTERM only, no SIGKILL escalation | item 18 |
| M17 | Fixed `login-bootstrap` deployment name | item 20 |
| M18 | Quarantine overwrites an existing file | item 19 |
| M19 | Assign `CODEX_HOME` without exporting it | items 22, 25 |
| M20 | Drop the data-dir lstat refusal | item 23 |
| M21 | Move the non-clobber guard inside the shared function | item 24 |
| M22 | Widen the sweep to "delete anything credential-status rejects" | item 24 |
| M23 | Attempt the history read on a non-Slack destination | items 15, 24b |
| M24 | `tool.yaml` reads the image `~/.codex` | item 25a |
| M25 | `tool.yaml` clobbers an OAuth credential | item 25a |
| M26 | The pasted code is accepted uncorrelated | R1 |
| M27 | A failed initial post re-attaches instead of re-asking | R2 |
| M28 | Ask-id recovery ignores the login kind | R5 |
| M29 | `--rebind` re-attaches to stale state | R3 |
| M30 | No outcome post when the login raises | R4 |
| M31 | Discord `message_id` ignored | R6 |
| M32 | A legacy DM destination treated as a channel | R8 |
| M33 | The CLI pre-check raises outside the handler | R7 |

## Review round

The house review contract plus a codex adversarial pass produced 15 candidates.
Nine verified; six were refuted as decisions the spec already records. Each fix
carries a test and a mutant.

| # | Finding | Severity | Test | Mutant |
|---|---|---|---|---|
| R1 | `_extract_code` applied no human-authorship and no anchor check, so a bot or an unrelated thread message in a shared login channel could supply the code written into the OAuth pty | BLOCKING | `T:test_the_pasted_code_is_correlated_like_the_ready_reply` (4 legs), `T:test_the_pasted_code_wait_uses_the_asks_anchor` | M26 |
| R2 | A failed **initial** ask post left a marker with no id; the next boot re-attached with an empty anchor no reply can match, wedging shared-channel login permanently | BLOCKING | `T:test_a_failed_initial_ask_post_does_not_wedge_the_next_boot` | M27 |
| R8 | A legacy Slack **DM** destination was classified as a channel, so it demanded an anchor a plain DM reply never carries. Ask-first could never start on `BOBI_LOGIN_CHANNEL=D...`, the only path that runs in production | BLOCKING | `T:test_a_legacy_destination_is_classified_by_its_id_shape`, `S:test_ready_predicate_handles_real_adapter_event` | M32 |
| R9 | Items 26/27 could not pass: they read `CODEX_HOME` in a sibling shell, where the entrypoint's export cannot reach, and masked the boot's exit with `\|\| true` | BLOCKING | rewritten around a `bobi` shim that dumps the manager's own environment | - |
| R3 | `--rebind` re-attached to stale state, landing in a thread ending in this bot's own success post, so the one recovery path for a revoked credential blocked to timeout | MAJOR | `T:test_rebind_posts_a_fresh_ask_over_stale_state` | M29 |
| R4 | A scrape or code-wait failure posted no outcome, leaving the thread on "Waiting for you to authorize" | MAJOR | `T:test_a_failure_after_the_ask_still_closes_the_thread` | M30 |
| R5 | Ask-id recovery matched the wording only, so one channel carrying two kinds' asks could adopt the wrong one | MAJOR | `T:test_ask_id_recovery_does_not_adopt_another_kinds_ask` | M28 |
| R6 | `_event_message_id` read only `fields.ts`; Discord names it `message_id`, so the watermark recorded nothing there | MAJOR | `T:test_a_discord_message_id_is_recorded_as_consumed` | M31 |
| R7 | An unknown `TOOL` raised outside the CLI handler and printed a traceback | MINOR | `T:test_cli_reports_an_unknown_tool_cleanly` | M33 |

`S:` = `tests/integration/test_login_bootstrap_smoke.py`, which is restored and
extended: it now generates the ask-first **ready reply** from the real Slack
adapter as well as the code event. That is what caught R8, and it closes the
"no end-to-end ask-first test" gap the review named.

### Refuted: decisions the spec already records

| Candidate | Why it is not a defect |
|---|---|
| No lock on the ask protocol; concurrent runs can double-post | D3: "prompt-only dedup. No lock." The residual cost is stated in spec §5 |
| The catch-up read cannot prove a human wrote the message | D9, Zach 2026-10-10: "this is an acceptable gap" |
| `CODEX_HOME` redirects codex away from `~/.codex/skills` | Accepted and stated in spec §5; nothing in the repo reads `${CODEX_HOME}/skills` |
| The api-key write's umask window and non-atomicity | Pre-existing; this change retargets the write without altering it |
| An event with no channel field fails open | Pre-existing predicate; R1's anchor check closes the practical exposure |
| `BRAIN_ENV` is not restored after the call | The spec's chosen retarget mechanism; the CLI is one-shot |
| PID may collide across containers | Deployments are bubble-scoped, so a collision needs one bubble; the spec names the residual |
