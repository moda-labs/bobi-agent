# Round 5 review: LEG B

Adversarial review of `plans/2026-08-04-fleet-repos-in-workspace-config.md`.

- Spec revision reviewed: `9e2e94e` ("scope the overlay to `subscribe:`, not `Config.load`")
- Session: `adhoc-db86da6e`
- Transcript: `0feba4b7-9635-4830-b76e-7eae35d62e26.jsonl`
- Launched: 2026-08-05T00:34:59Z
- Status: returned in time; folded into 3aeb9a9 / 74d8642 / a01c1cf

Verbatim reviewer output follows.

---

Verification complete. Every load-bearing citation read first-hand in the worktree.

---

# Adversarial review: `plans/2026-08-04-fleet-repos-in-workspace-config.md`

## Part 1: Citation verdicts

**CORRECT** (verified against the tree, not re-stated from the spec):

| Claim | Verdict |
|---|---|
| `package/agent.yaml:117` subscribe, `:125` managed_repos, `:130` `agent: moda-eng-team`, `:78` auto_dispatch, `:101-104`/`:105-110` dispatch entries | CORRECT. The overlay example matches the live pack list-for-list, in order. |
| `Config.load` is uncached: `config.py:551-556` → `_parse` → `_load_yaml` `:243-246` bare `read_text()` | CORRECT. No memo anywhere in the module. |
| `_parse` holds monitors/requires/host/build uninterpolated (`config.py:564-571`) | CORRECT. Old B4 is real and is now moot. |
| `install.py:118-136` seed_workspace copy-only-if-absent | CORRECT, and stronger than claimed: it is a per-file `rglob`, so the template lands into the already-existing `<run>/workspace/`. |
| Withdrawal of the `paths.py` "walks for that filename" claim; `:123-125`, `:106`, `:112`, `:132`, `:140` | CORRECT. The retraction is accurate. |
| `paths.py:23` ROOT_MARKER, `:71-72` global_config_path, `config.py:4-5` | CORRECT. The `config.yaml` collision is real. |
| Validation belongs at `_load_config_or_raise` (`service.py:248`, called `:484`), not `:501` | CORRECT, and reachable on **both** start paths (`spawn_team:348` too). The M3 fix holds. |
| `subagent.py:106-113`, `:1422-1431`, `:1435`, `:1473-1474`, `:1483`, `:1486`, `:1509-1511`, `:1591-1592` | ALL CORRECT, including the correction that `:1486` calls `_register_with_retry` and does not unlink the cursor. |
| `env.py:34` + `:25-27`, `setup/actions.py:164`, `script_cache_checks.py:649` | CORRECT. |
| `cli.py:2417-2419` monitors list | CORRECT, and an apt precedent (merged view + source column). |
| 42 `Config.load(` hits, `build_render.py:248` is a docstring, so 41 real | CORRECT. |
| Reader grep reproduces exactly the 13 hits the table classifies; nothing invented | CORRECT. |
| `events/server.py:612`, `client.py` `_needs_resubscribe` (:419-425), `index.ts:650`, `Dockerfile:283`, `fsutil.py` #951, `setup/services.py:307`, `managed_repos` absent from `docs/` and `bobi/` | CORRECT. |
| `doctor.py:253` is a layout existence check | CORRECT in substance (function opens at `:244`, not `:245`). |

**WRONG:**

1. **"`find_env_var_refs` (`config.py:114-141`) drives what `bobi validate` requires."** `validate_config` (`bobi/validate.py:105-127`) never calls it. There is no `bobi validate` command; the only `validate` is `bobi/cli.py:2343` (`workflows validate`). The real consumers are `bobi/cli.py:753` (install-time `.env` scaffold) and `bobi/setup/actions.py:384` (`missing_credentials` in the setup wizard).
2. **"`monitors/registry.py:80` ... not a content read."** `_read_records` (`bobi/monitors/registry.py:38-48`) does `yaml.safe_load(path.read_text())` and returns `raw.get("monitors")`. It is a content read of `monitors:`, making the honest content-reader count seven, not six.
3. **"`subscribe:` is read at exactly two sites, both during manager startup."** `ingress.py:109` is reached from `build_startup_info` (`service.py:185`, the spawning CLI parent, inside `except Exception: log.debug` at `:192-193`) and from `bobi doctor` (`doctor.py:541`). Neither is manager startup. Plus `snapshot.py:99` on every heartbeat, which the spec itself names 60 lines later.
4. **"The overlay can never carry `build:` (it is not in the applied set)."** Contradicted by the spec's own table at line 123: non-applied keys are *carried as data*. Not-applied is not not-carried.
5. **"Verified: `managed_repos` has zero consumers ... the PR carries two changes, not one."** See BLOCKER 2.

**MISLEADING:** `snapshot.py:48`/`:105` "emit an empty heartbeat" (the heartbeat is emitted; `:48` nulls the team version, `:105` empties the monitors list). "Consumption is order-insensitive (`service.py:543-545`, `:549-551`)" - those prove *appending* is order-insensitive, nothing more. `drain.py` "eight times" - 10 occurrences on 9 lines. The review record's "`setup/actions.py` 169 to 171" contradicts its own table's `:164`.

## Part 2: Did the reversal close the four blockers?

- **B1 CLOSED by construction.** `Config.load` never sees the overlay; the applied set is read once at `service.py:534`.
- **B2 NARROWED then MISCHARACTERIZED.** Six live readers become one: `snapshot.py:99`, every heartbeat, with no boot parse behind it. But the spec reasons only about the valid-overlay case. On a torn overlay, `discover_subscriptions`' own `except Exception: pass` (`subscriptions.py:44-45`) swallows and falls through to `Config.load` → `event_services` → `[project_path.name]`. The heartbeat then reports a *fabricated* set (literally `["run"]` on this deployment), not "the intended set." Silence detection diffs that against traffic, so every real topic reads as unexpected. The "one honest exception" paragraph understates its own exception.
- **B3 CLOSED as an inventory** (mechanically derived, all 13 hits classified), **with one wrong row** - see MAJOR 7.
- **B4 CLOSED.** No `Config._parse` interaction exists.

## Part 3: Findings

### BLOCKER 1 - `subscribe: []` in the overlay does not mean "no subscriptions". It silently auto-detects.
`bobi/events/subscriptions.py:42-43` is `if explicit: return explicit`. An empty list is falsy, so a *defined but empty* `subscribe:` skips the explicit branch entirely and falls through to `cfg.event_services` auto-detection (`:47-55`), then to `[project_path.name]` (`:57`).

The spec's merge rule ("a top-level key present in the overlay replaces the pack's value") makes this reachable in the single most obvious operator action the feature exists for. The spec's boot validation checks "each applied key has the shape its consumer expects" - a list is a valid shape, so an empty list passes.

**Failure scenario:** the operator offboards the last two repos, leaving `subscribe: []` (or comments the entries out, or a `mv`-over lands a file whose list YAML-parses empty). Restart. `discover_subscriptions` returns the auto-detected adapter keys for the pack's three `events: true` services (`package/agent.yaml:6-23`: github, slack, linear). The fleet keeps ingesting Slack and Linear and drops GitHub, with no error and a green boot. This is precisely "the fleet would run half-migrated with no signal," the consequence the reversal was written to eliminate. Not named in the spec, not in the verification plan (`replace` and `absent-key fallthrough` are tested; empty-but-present is not).

### BLOCKER 2 - The blocking co-deliverable is under-scoped. A live CI gate in the target repo reads both moved keys.
The spec asserts `managed_repos` has no consumers and concludes the moda-agents PR "carries two changes, not one." `moda-labs/moda-agents scripts/check-deploy-compose.py:85-94` builds `managed_repos` from the composed `agent.yaml` and fails the build unless `moda-labs/moda-skills` maps to `github-issues`; `:79-83` fails unless `subscribe:` contains `github:moda-labs/moda-skills`. It is wired into CI at `.github/workflows/lint.yml:57`. Its docstring (`:2`) states its purpose: "Lint deployed teams after the same compose step `bobi deploy` uses."

**Failure scenario A:** post-ship, the operator offboards moda-skills by deleting its lines from the overlay and restarting. The fleet stops managing it. moda-agents CI still reads the pack's now-vestigial lists, still finds both entries, and reports green - asserting an invariant about the deployed team that is no longer true. The guard now lints a dead file.
**Failure scenario B:** a later engineer tidies the pack lists the spec has made inert. moda-agents CI goes red over a list that governs nothing, on a repo where nothing changed semantically.

### BLOCKER 3 - Fail-closed boot validation on an agent-writable file is a supervised crash-loop with no specified recovery.
The spec makes three choices that compose badly and never reasons about their composition: (a) the overlay is operator-writable "by construction" (line 212); (b) an LLM agent, the director, is explicitly authorized to write it (lines 470-477); (c) a malformed overlay "fails the boot loudly" (line 362), asserted again as an integration test (line 568).

In production the manager is not a foreground shell. `bobi agent <n> supervise` (`cli.py:384-410`) spawns it as a supervised child, which re-execs `start --foreground` → `run_team_foreground` → `_load_config_or_raise`. A manager that dies at boot is a *fast crash* (`supervision.py:494-518`), charges the restart budget, backs off, and on exhaustion returns `EXIT_BUDGET_EXHAUSTED = 70` (`supervision.py:68`, `:512`), which the orchestrator turns into a machine restart.

**Failure scenario:** the director writes a truncated or schema-wrong overlay (the spec concedes at lines 347-349 that it cannot guarantee otherwise). The next manager restart hard-fails, the supervisor crash-loops, exits 70, Fly restarts the machine, repeat. Recovery requires editing a file on a machine that is crash-looping, and the same `for_agent` guard the spec adds for pack-swap safety (lines 375-380) has the identical shape. The spec never states the fail-open alternative (log a required-attention error, boot on the pack, refuse to apply the overlay), never names the exception type, and never checks that `cli.py:450-460`'s handler list catches it - today an unmatched exception from `_load_config_or_raise` surfaces as a raw traceback, not a named failure.

### MAJOR 4 - The `find_env_var_refs` union is machinery bought for a path this workflow never takes.
Its stated justification is wrong (see WRONG 1). Its actual consumers run at install (`cli.py:753`) and setup (`setup/actions.py:384`), never at start. The overlay's entire value proposition is "no reinstall."

**Failure scenario:** the operator adds `- github:${NEW_ORG}/repo` to the overlay and restarts, as instructed. Nothing prompts for `NEW_ORG` (that only happens at install). `_interpolate_env` resolves the unset var to `""` (`config.py:230-235`), producing the topic `github:/repo`, which `_normalize_explicit_subscriptions` keeps because it is non-empty. The manager subscribes to garbage. The union change cannot fire on this path. This is a chunk of scope whose benefit the design's own premise removes, and it is the one place the framework side is still too wide.

### MAJOR 5 - `config show` reports intent, not effect. Nothing confirms an onboard succeeded.
The operator loop is: edit overlay, restart, "did it work?" The one read-back the spec ships answers "what does the file say," which the operator just typed. The supervisor's `_expectations` (`snapshot.py:88-99`) is also intent - it is `discover_subscriptions` again.

Effect can legitimately diverge from intent on exactly the onboarding path: `authorize_resources` (`events/server.py:612`) drops a `github:` topic whose #488 grant is missing or rejected, and its own docstring says so (`:624-626`, "logged LOUDLY and DROPPED"). A log warning in `state/manager.log` is the only signal.

**Failure scenario:** the operator onboards `moda-labs/newrepo`, restarts, runs `config show --json`, sees `managed_repos` and `subscribe` both containing it, and reports success. The grant failed, the topic was filtered before registration, and no event ever arrives. The spec's operator consequences (lines 431-439) cover stranded runs and backlog fan-out but not "verify the topic actually registered."

### MAJOR 6 - The `config show` contract omits the two things that matter for a credential-bearing document.
The spec specifies the envelope (`{"key": {"value": ..., "source": ...}}`) and calls the output "a contract, because a frozen prompt parses it." It never says whether `value` is interpolated, nor whether anything is redacted. The document it prints carries `services[].credentials` (`package/agent.yaml:10-23`: `${GH_TOKEN}`, `${SLACK_BOT_TOKEN}`, `${SLACK_SIGNING_SECRET}`, `${LINEAR_API_KEY}`).

The loader contract says "merged **uninterpolated**," but the command is described as printing "the **effective** document," which reads the other way. An implementer resolving that ambiguity toward "effective" ships a live-credential dump to stdout - into a director transcript, and plausibly into the Slack channel it reports to. This is the one spot where the spec's own rejected-design reasoning ("an unsupported-but-reachable surface on the credential path is the wrong shape," lines 236-241) applies to its own chosen design and is not applied.

### MAJOR 7 - The table the spec declares to be "the invariant, and it is a test" contains a wrong classification.
`registry.py:80` is marked "not a content read" (see WRONG 2). The spec makes a point of the last three revisions' inventories coming back incomplete, then ships a misclassified row in the artifact it elevates to an invariant.

**Failure scenario:** a maintainer wants `monitors` in `OVERLAY_APPLIED_KEYS` (the obvious next ask). The table tells them the monitor registry does not read agent.yaml content, so overlay monitors will just work. They do not: `MonitorRegistry` reads `package/agent.yaml` directly (`:80`) and drives the scheduler (`service.py:540-542`), while `snapshot.py:106` reads `cfg.monitors` from `Config.load` - the two would diverge, which is the `env.py:25-27` bug class the test exists to prevent. The specified test happens to catch this transitively (`Config.load` also parses `monitors:`), so the guard survives by luck, not by the enumeration.

### MAJOR 8 - The co-deliverable ordering is unstated, and the natural order breaks the director.
moda-agents deploys bobi as a released PyPI pin, not a checkout (`bobi-deploy/.../scaffold.py:167`, `pip install "bobi==${BOBI_VERSION}"`), and `deployments/eng-team.yaml:15,27` carries the established `Requires BOBI_VERSION >= X` + bump convention. The prompt text instructs the director to run `bobi agent <n> config show --json`.

**Failure scenario:** the moda-agents PR merges and deploys before a bobi release carrying `config show` is pinned. The director runs a command that does not exist, gets a non-zero exit and a usage error, and either falls back to `package/agent.yaml` (the stale list the change exists to retire) or blocks. The spec names the co-deliverable blocking in one direction only and never states the release-and-pin step or the `BOBI_VERSION` bump.

### MINOR 9 - Self-contradictions
"Exactly two sites, both during manager startup" (line 353) vs. the supervisor heartbeat exception (line 415). "The overlay can never carry `build:`" (line 325) vs. the design table's data-key rule (line 123).

### MINOR 10 - Headline claims do not match the document
"Add two lines to one workspace file" (line 94): onboarding a managed repo is three lines across two keys per the spec's own example (`- github:owner/repo`, `- repo: owner/repo`, `  tracker: github-issues`). "Complexity is now small" (line 596) sits above an 8-step implementation plan, a new module, a new CLI group with three output modes and a parsed contract, and a 16-bullet verification plan with five integration tests.

### MINOR 11 - An open plan in the target repo covers these exact keys and is unreferenced
`moda-agents plans/2026-08-03-baohua-eng-separation.md:190-192` and `:325-333` govern `moda-eng-team`'s `subscribe:` and `managed_repos:`, with `:331-333` still unchecked and asserting a *different* live-effect mechanism ("replaced server-side by `register` on the next manager-session start") than the spec's PUT-preserves-identity argument (lines 425-428). Per this repo's plan convention the plan file is the source of truth; the spec should say whether it amends or supersedes those items.

### MINOR 12 - `docs/BUILDING_AGENT_TEAMS.md:418-419` becomes stale
It currently rules: "If your team needs a knob, make it an env reference; if it needs domain content, make it a workspace file." The overlay adds a third category. Step 7 adds a new section but does not say to amend the existing rule it partially invalidates.

## Answers to the lens questions

**Too wide or too narrow?** The framework side is now correctly narrow - the reversal is right and genuinely closes B1 and B4. One piece of leftover machinery is still too wide (MAJOR 4: the union, whose cited consumer does not exist and whose real consumers never run on this workflow). The operational side is still too narrow in three places, and two of them (BLOCKER 1, BLOCKER 3) are silent-wrong-state and take-the-fleet-down respectively. So: over-corrected on framework purity in one spot, still under-specified operationally.

**Is the applied-vs-data split honest?** Mostly yes, and the boot log line naming applied vs. carried keys is the right mitigation for the `services:`/`brain:` footgun. But it has one live hole in the *applied* key itself (BLOCKER 1), and the spec's own "can never carry `build:`" shows the author does not hold the model consistently.

**Does `config show` have a real output contract?** Half of one. The envelope is specified; the value semantics on a credential-bearing document are not (MAJOR 6), and it answers the wrong question for the operator's actual loop (MAJOR 5).

**Is the blocking co-deliverable correctly scoped?** No - at least three pack-side changes, not two (BLOCKER 2), with an unstated release-ordering dependency (MAJOR 8).

**Would a human operator succeed with only this document?** Not reliably. They would know which file to edit and that a restart is required. They would not know which restart command applies under the supervisor, how to confirm the change took effect, that an empty list means auto-detect rather than nothing, or what to do when a bad edit puts the machine in a crash loop.

---

**VERDICT: NOT IMPLEMENTABLE** - three blockers (empty-`subscribe:` fallthrough, under-scoped co-deliverable with a live CI consumer, unexamined fail-closed crash-loop on an agent-writable file) plus a wrong load-bearing citation about `bobi validate` that invalidates the justification for one whole section.
