# Round 5 review: LEG C

Adversarial review of `plans/2026-08-04-fleet-repos-in-workspace-config.md`.

- Spec revision reviewed: `9e2e94e` ("scope the overlay to `subscribe:`, not `Config.load`")
- Session: `adhoc-0b6ba091`
- Transcript: `6819276b-7197-44fb-b148-3304a95765b3.jsonl`
- Launched: 2026-08-05T00:45:24Z
- Status: ORPHANED. Its launcher (wf-adhoc-eng-team-952-spec-rework) exited at ~00:53 while this leg was still running, so its findings reached nobody. Recovered from the transcript on 2026-08-05.

Verbatim reviewer output follows.

---

Read the spec and verified every load-bearing citation against the worktree. Here is the review.

## Citation verdicts

| Citation | Verdict |
|---|---|
| `install.py:118-136` seed_workspace, copy-only-if-absent | **CORRECT** (`install.py:126-136`, `elif not target.exists()`) |
| `paths.py:23` ROOT_MARKER, `:71-72` global_config_path, `:106`/`:112`/`:132`/`:140` exact-path `is_file()`, `:123-125` "cwd walk-up is intentionally gone" | **CORRECT**. The withdrawal of the fabricated walk-up justification is accurate |
| `config.py:4-5` `<home>/config.yaml` deliberately limited | **CORRECT** |
| `config.py:551-556` Config.load, `:243-246` `_load_yaml` bare `read_text()`, no cache in module | **CORRECT** |
| `subscriptions.py:34`, `:38-40`, `:44-45`; `ingress.py:62`, `:109` | **CORRECT** |
| `service.py:248`/`:484`, `:534`, `:543-545`, `:549-551`, `:664` | **CORRECT** |
| `env.py:25-27` divergence warning, `:34` | **CORRECT** |
| `snapshot.py:88-99` `_expectations`, `:105`; `telemetry.py:96` | **CORRECT** |
| `subagent.py:106-113`, `:1422-1431`, `:1435` unlink, `:1473-1474`, `:1483`, `:1509-1511`, `:1591-1592`; the `:1486` correction | **CORRECT** (all seven, including the correction) |
| `cli.py:2417-2419` `monitors list` precedent | **CORRECT** |
| `doctor.py:253` inside `_check_runtime_layout`, no content read; the `doctor.py:253` withdrawal | **CORRECT** |
| `script_cache_checks.py:649`, `setup/actions.py:164`, `events/server.py:612`, `Dockerfile:283` | **CORRECT** |
| pack `:78`, `:101-104`, `:105-110`, `:117`, `:125`, `:130`; `managed_repos` zero consumers in pack; pack has no `workspace/` | **CORRECT** (verified against the live `-r--r--r--` image) |
| `monitors/registry.py:80` "not a content read" | **WRONG.** `_read_records` (`registry.py:38-48`) does `path.read_text()` and returns `raw.get("monitors")`. It is a content read, and it runs at manager boot via `service.py:541` |
| "41 real `Config.load` call sites" | **WRONG.** 40. `cli.py:188` is a second docstring line, alongside `build_render.py:248` |
| "eight times in `events/drain.py`" | **WRONG.** `grep -c tracker` returns 9 |
| `find_env_var_refs` (`config.py:114-141`) | **MISLEADING.** The function is `114-130`; `141` is inside `find_required_env_vars` |
| `_scan_env_refs` (`:190-197`) | **MISLEADING.** `190-196` |
| `snapshot.py:48` "emit an empty heartbeat" | **MISLEADING.** `:48` is `_team_package_version`; it yields a null `versions.team_package`, not an empty heartbeat |
| Review record: "`setup/actions.py` 169 to 171" | **WRONG**, and contradicts the spec's own table. The line is 164 |

---

## BLOCKER 1 - the reversal MOVES prior blocker 2, it does not close it

Spec:353-357 says `subscribe:` is read "at exactly two sites, both during manager startup," and :371-373 concludes the bare `except Exception: pass` "is now correct rather than a hazard, because the overlay it would swallow has already been parsed successfully at boot."

That reasoning is boot-scoped. The supervisor sidecar is not.

- `bobi/supervisor/snapshot.py:99` calls `discover_subscriptions` inside `_expectations`.
- `_expectations` runs on every heartbeat (`snapshot.py:161` inside `build_heartbeat`, published at `telemetry.py:169`).
- Cadence: `bobi/supervisor/config.py:61`, `poll_interval: float = 30.0`.
- Different process: the sidecar is container PID 1 (`docker/docker-entrypoint.sh:630`).

Failure scenario: manager running, overlay valid. Operator opens `<run>/workspace/overlay.yaml` in an editor that truncates before writing (the spec concedes at :347-349 that nothing prevents this). The sidecar's 30s tick lands in the truncated window. `load_agent_yaml` raises inside `subscriptions.py:36`'s `try`, is swallowed at `:44-45`, and control **falls through** to `Config.load` (`:48`), then to `return [project_path.name]` (`:57`). The heartbeat publishes `expectations.subscriptions: ["run"]`. The read model diffs that against traffic: all seven real topics read as unexpected, `run` reads as silent.

The swallow never reaches `_expectations`' own `except`, so the docstring promise at `snapshot.py:92-93` - "an error yields empty lists (silence detection simply has less to assert)" - is broken. Not a degraded read; a garbage read that looks well-formed.

This is verbatim the spec's own consequence 2 at :227-234 ("A torn read would degrade live processes silently"), listed as one of the three reasons the previous revision was invalidated. The reversal shrank the site count from 40 to 1 and mistook that for closure. The "one honest exception" paragraph (:414-423) describes only the successful-parse case. No test in the verification plan covers it: both malformed-overlay tests (:534-537, :568-570) are boot-only.

## BLOCKER 2 - the frozen prompt states a rule the design does not implement

The verbatim prompt at spec:473-477 tells the director:

> A top-level key present there replaces the same key in `run/package/agent.yaml`.

No applied-key restriction. The spec states the rule the same universal way twice more (:74 in the seed template, :143-144 in bold) and the restricted way twice (:75, :121-123). Template lines :74 and :75 contradict each other in adjacent lines.

Failure scenario: director is asked to drop the engineer to a cheaper model. Following its own frozen prompt, it writes `roles: {engineer: {model: claude-sonnet-5}}` into the overlay, `mv`s it in, restarts. `roles` is not in `OVERLAY_APPLIED_KEYS`, so `Config.load` still parses the pack's `roles:` (pack `agent.yaml:113-116`, `model: claude-opus-5`, `effort: max`). Every engineer keeps running Opus at max effort. Nothing fails. The only signal is one boot log line in a detached daemon's `state/manager.log`.

Same shape for `services:` (credentials), `brain:` (`env.py:34`), `monitors:` (`registry.py:83` and `config.py:624`). The spec's own argument against `Config.load` (:236-241) was that an "unsupported-but-reachable surface on the credential and identity path" is the wrong shape. The chosen design keeps it reachable and makes it silently inert, which is not obviously better: an inert credential override is indistinguishable from a working one until something breaks.

The fix is one clause, and the spec already computed the input for it. Boot validation "checks three things and no more" (:367-369); make it four: reject a top-level overlay key that is outside `OVERLAY_APPLIED_KEYS` **and** parsed by the framework (the union of `Config`'s fields and the four raw readers' keys). `managed_repos` and `for_agent` pass; `roles`/`services`/`brain`/`monitors` fail loudly at the boot that follows the edit. Without it, the spec's "Data keys are not silently ignored" (:137) is not true in any operationally useful sense.

## BLOCKER 3 - the headline onboarding promise is false for the case the feature exists to serve

Spec:425-428 reassures the operator that a restart is cheap because "the saved-deployment path PUTs the newly authorized list against the existing `deployment_id` (`bobi/subagent.py:1422-1431`) with the event cursor intact, so events arriving during the gap replay rather than drop."

That holds only when the PUT succeeds. Trace the restart-after-onboarding path:

1. `subagent.py:1464`: the saved-deployment path calls `authorize_resources(..., filter_unauthorized=False)`.
2. `events/server.py:718-725`: when the grant is denied ("the configured %s credential cannot read it"), `filter_unauthorized=False` **keeps** the topic.
3. `subagent.py:1431`: `resp.raise_for_status()` on the PUT. The server hard-rejects the whole update on a missing #488 grant.
4. `subagent.py:1433-1436`: `except` → **`cursor_path.unlink(missing_ok=True)`** → `_register_with_retry` → a new `deployment_id`/`api_key`.

Failure scenario: operator adds `github:moda-labs/newrepo` to the overlay's `subscribe:` and restarts, before the GitHub App is installed on that repo. Result: the event cursor is deleted and the deployment identity is re-minted. Events arriving during the gap are **dropped, not replayed** - the exact opposite of what :425-428 promises. One WARNING line in the manager log.

The spec cites `:1435` (the unlink) at :448-449, in the deferred live-reload section, so it knows the line exists. It never connects it to the restart flow that is the entire point of the document. And the verification plan's grant test (:570-572) asserts "an ungranted topic is reported rather than silently dropped" - that describes the `filter_unauthorized=True` branch, which the restart path does not take.

Two things must be added: the prerequisite (the repo's GitHub App install / credential coverage must exist **before** the overlay edit), and the failure mode when it does not.

## MAJOR 1 - "exactly two sites, both during manager startup" is false four ways

Under the design, `subscribe:` is read through two functions with four call paths:

- `discover_subscriptions`: `service.py:534` (boot, validated) and `snapshot.py:99` (sidecar, 30s, unvalidated).
- `explicit_subscriptions`: reached from `ingress.py:109` in `check_ingress_reachability`, whose callers are `doctor.py:541` and `service.py:185`.

`doctor.py:541` is `bobi agent <name> doctor`, run on demand by an operator, nowhere near boot. Exactly one of the four is the validated boot. The spec's entire failure-handling argument ("caught once, at boot") rests on the false count, and the spec contradicts itself 60 lines later by naming the sidecar as a third site.

## MAJOR 2 - the reader table is called "the invariant, and it is a test," and one row is wrong

Spec:301 elevates the table to a test. Row `monitors/registry.py:80` is classified "passes a `Path` into `_read_records()` | not a content read." Verified: `_read_records` (`registry.py:38-48`) reads the file and extracts `monitors:`, and `MonitorRegistry.load(project_path=...)` runs at manager boot (`service.py:541`), three lines above the `subscribe` list it feeds.

Consequence: the guard test at :542-544 lists `env.py:34`, `setup/actions.py:164`, `script_cache_checks.py:649` and `Config.load` - not `registry.py`. The `Config.load` clause happens to cover `monitors` (`config.py:414`, `:624`), so the hole is closed by accident, not by design. Meanwhile `monitors:` is the single most plausible next widening request (adding a monitor without a pack PR is the same operator need as adding a repo), and the table tells the next reader it is not a reader at all.

## MAJOR 3 - `config show` has a JSON shape but not a usable contract

The spec is right that a frozen prompt parsing the output makes the format a contract (:389-393). Two gaps remain:

- **The command does not exist as specified.** `bobi/cli.py:3557-3567` attaches subcommands to the `agent` group from two **hardcoded lists**. A new `@main.group() def config()` following the cited `monitors list` precedent (`cli.py:2417-2419`) is not reachable as `bobi agent <name> config show` until `"config"` is added to the list at `cli.py:3565`. The spec cites the decorator and not the attach point. `config` is otherwise free - no collision.
- **The prompt cannot execute what it is told.** :475 hands the director the literal `bobi agent <name> config show --json`, with no instruction for resolving `<name>`, and no JSON path for the value it wants. Under the specified shape that is `.managed_repos.value`, not `.managed_repos`. For a frozen, blocking co-deliverable, both belong in the spec.

## MINOR 1 - the mechanically-derived counts are still wrong

Spec:268-270 records the commands "so the next reader can re-derive it rather than trust the table." Re-derived: `grep -c "Config\.load("` returns 42; the spec accounts for one docstring and claims 41 real. `cli.py:188` ("effect inside ``Config.load()``.") is a second docstring. 40 real. Separately, `grep -c tracker bobi/events/drain.py` returns 9, not the "eight" at :555. Fourth revision, third wrong count.

## MINOR 2 - the review record contradicts the spec's own table

:618 claims round 4's `setup/actions.py` citation "moved 169 to 171." The table (:285) and the code both say `:164`; 169 and 171 are inside `installed_team_name`'s return statements. A spec whose credibility rests on "every one of its citations was re-verified first-hand" (:616) should not disagree with itself about a line it verified.

## MINOR 3 - "the overlay can never carry `build:`" is the wrong statement

:325-326. The overlay can carry a `build:` key; it is never *applied*. Since `_scan_env_refs` is a raw-text regex (`config.py:196`), a `${VAR}` under an inert overlay `build:` block is scanned, joins `elsewhere`, and becomes runtime-required for a variable nothing runtime uses. Over-requiring, so safe by the spec's own stated direction (`config.py:154-156`) - but the premise as written is false and the implementer will discover it.

## MINOR 4 - the "unreachable placement" argument is circular

:361-365 rejects `run_manager_from_config:501` because `_load_config_or_raise` at `:484` "would have failed earlier." That is only true once validation is *already* at `_load_config_or_raise` - which is the conclusion, not the premise. The previous revision put validation at `:501`, so `:484` had nothing to fail on.

The conclusion is right, and there is a real argument for it the spec never makes: `_load_config_or_raise` has three callers (`service.py:348` `spawn_team`, `:430` `start_team`, `:484` `run_team_foreground`), which covers detached start, waited start, and foreground; and the production container path reaches it (`docker-entrypoint.sh:630` → `supervise` → `bobi ... start --foreground` at `service.py:399-403` → `:484`). Use that.

---

## Verdict on the lens questions

**Too wide?** No, not any more. The reversal is genuinely correct on the framework axis: one applied key, zero repo vocabulary in `bobi/`, `seed_workspace` untouched, no `repos` CLI, no branch-delete machinery, no promotion of `bobi-agent`/`moda-agents`. All six rulings are obeyed. Ruling 5 in particular is obeyed on Zach's own test - `config show` is a generic command, and it has a real precedent at `cli.py:2417`.

**Too narrow?** Yes, and worse than the prior round. The prior round's blockers were about live-process exposure. The reversal reduced the surface from 40 sites to 1 and treated the reduction as elimination. Blocker 2 is **moved**, not closed: it now lives at `snapshot.py:99` on a 30-second timer in a separate process, double-swallowed, with a garbage-value fall-through rather than an empty one - a narrower target and a worse failure. Blockers 1, 3 and 4 are genuinely closed.

**Is the applied-vs-data split honest?** Not as written. The spec states the merge rule universally in four places, including the frozen prompt, and restricts it in two. The detection mechanism is a boot log line in a detached daemon. A one-clause boot rejection is available and not taken.

**Would an operator succeed onboarding a repo from this document alone?** No. The document never names the GitHub-grant prerequisite, and the restart it prescribes destroys the event cursor when that prerequisite is missing (`subagent.py:1435`) - while an earlier paragraph promises the opposite.

**NOT IMPLEMENTABLE** - three blockers: a live 30s unvalidated overlay read in the sidecar that the spec claims does not exist, a frozen prompt stating a merge rule the design does not implement, and a headline onboarding promise falsified by the cursor-unlink path the spec cites elsewhere.
