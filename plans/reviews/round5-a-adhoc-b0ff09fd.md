# Round 5 review: LEG A

Adversarial review of `plans/2026-08-04-fleet-repos-in-workspace-config.md`.

- Spec revision reviewed: `9e2e94e` ("scope the overlay to `subscribe:`, not `Config.load`")
- Session: `adhoc-b0ff09fd`
- Transcript: `ba3dadb1-e99b-4a44-b694-1c5283e79e04.jsonl`
- Launched: 2026-08-05T00:34:59Z
- Status: returned in time; folded into 3aeb9a9 / 74d8642 / a01c1cf

Verbatim reviewer output follows.

---

I read the spec and verified every load-bearing citation first-hand in the worktree, re-ran both of its "mechanically derived" greps, and executed the critical path rather than reasoning about it.

---

# Adversarial review: `plans/2026-08-04-fleet-repos-in-workspace-config.md`

## Citation audit

I checked every load-bearing citation. The great majority are **CORRECT**, and the spec's self-withdrawals (the fabricated `paths.py` walk-up, the `doctor.py:253` mischaracterization, the `subagent.py:1486` cursor claim) are honest and verified right. Specifically confirmed correct: `paths.py:23/71-72/106/112/123-125/132/140`; `config.py:4-5/114/144-172/154-156/190/243-246/281/551-556/668/684`; `service.py:248/474/484/488/501/534/543-545/549-551`; `subscriptions.py:34/38-40/44-45`; `ingress.py:62/109`; `env.py:25-27/34`; `setup/actions.py:164`; `script_cache_checks.py:649`; `doctor.py:253`; `subagent.py:106-113/1422-1431/1435/1473-1474/1483/1486/1509-1511/1591-1592`; `snapshot.py:88-99`; `telemetry.py:96`; `events/server.py:612`; `cli.py:2417-2419`; `install.py:118-136`; `fsutil.py`; `Dockerfile:283`; `package/agent.yaml:78/101-104/105-110/117/125/130`; the moda-agents pack having no `workspace/`; `managed_repos` having zero consumers in `bobi/`, in the pack beyond `:125`, and in `docs/`.

Four are **WRONG**:

| Claim | Verdict |
|---|---|
| "`grep -c` returns 42 but `build_render.py:248` is a docstring line, so 41 are real" | **WRONG. 40 are real.** `bobi/cli.py:188` (`effect inside ``Config.load()``.`) is a second docstring line matching the same grep. |
| `bobi/monitors/registry.py:80` -> "not a content read" | **WRONG.** `_read_records` (`bobi/monitors/registry.py:38-48`) does `yaml.safe_load(path.read_text())` and returns `raw.get("monitors")`. It is a raw content read of the `monitors:` key, one indirection down. |
| Review record: "`setup/actions.py` 169 to 171" | **WRONG**, and self-contradictory: the real line is `164`, which is what the spec's own table says. |
| "`tracker` ... eight times in `events/drain.py`" | **WRONG**, 9 matching lines. Trivial. |

Three are **MISLEADING**:

- "`subscribe:` is read at exactly two sites, **both during manager startup**." The `ingress.py:109` read sits inside `check_ingress_reachability`, which is called from `doctor.py:541` (`bobi doctor`, arbitrary time) and `service.py:185` (inside `build_startup_info`, called from `cli.py:48` and `service.py:408`). `discover_subscriptions` is additionally called from `snapshot.py:99` on every supervisor heartbeat. Five call paths, three of them not boot.
- "Every hit is classified below." True of grep 1 (13 hits -> 11 rows, complete). But grep 2's 42 hits are **counted, not classified** - collapsed into one row (`config.py:281`). That is exactly where `bobi/events/subscriptions.py:48` was lost, and it is the fallback branch of the primary apply site. See BLOCKER 1.
- `find_env_var_refs` cited as `config.py:114-141`; the range spans into `find_required_env_vars` (`133-141`). `_check_runtime_layout` cited as `doctor.py:245-258`; def is at `:244`.

---

## BLOCKER 1 - replace-semantics + an empty `subscribe:` silently auto-subscribes from git remotes

`bobi/events/subscriptions.py:42` gates on truthiness (`if explicit:`), and `:47-57` is a fallback that calls `Config.load` -> `detect()`. The spec never classifies `:47-57`.

Under replace semantics, an overlay `subscribe:` that merges to empty does not mean "subscribe to nothing" - it means "fall through to auto-detection". `_detect_github` (`bobi/events/adapters.py:59-76`) walks the run root's immediate children and derives `github:<slug>` from each child's git remote.

Reproduced by execution (the merged value injected at the read site, since the merge helper does not exist yet):

```
BASELINE (pack subscribe: present): ['github:moda-labs/moda-skills', 'linear:MOD']
CASE A  (merged subscribe: []):     ['github:moda-labs/familystories-ai']
CASE B  (torn document):            ['github:moda-labs/familystories-ai']
```

Failure scenario: an operator offboards the last GitHub repo by commenting out the entries, or writes `subscribe:` with the list mis-indented (YAML `None`). Boot validation as specified passes - "each applied key has the shape its consumer expects", and `[]` is a list. The manager then subscribes to whatever `run/repo` and any sibling checkout point at, and silently drops `slack:` and `linear:` entirely.

This falsifies the spec's central claim:

> It closes the resurrection trap by construction ... No tombstones, no second mechanism.

There *is* a second mechanism, and the overlay's own replace rule is what arms it. The live deployment has `services: github/slack/linear` all with `events: true` (`package/agent.yaml:6-23`), so `cfg.event_services` is non-empty and the fallback fires. Note also that the two apply sites disagree here: `ingress.py:75-79` returns `[]` with no fallback, `subscriptions.py:42` falls through.

---

## BLOCKER 2 - a torn overlay is swallowed into that same fallback, not failed loudly

CASE B above is the proof. `subscriptions.py:44-45` catches the merge error and falls through to auto-detection. The spec's justification for keeping that bare except is circular:

> that is now correct rather than a hazard, because the overlay it would swallow has already been parsed successfully at boot

The write side of the same spec explicitly disclaims that premise ("a human with an editor can still perform a non-atomic write, and no framework change prevents that"), and `snapshot.py:99` re-reads on every heartbeat, hours after boot, in the **supervisor process**, which never calls `_load_config_or_raise`. So B2 is relocated, and the blast radius is worse than before: `snapshot.py:92-93` promises "an error yields empty lists", but no exception escapes - the heartbeat publishes a **wrong non-empty** expectations set, which the read model diffs against traffic to derive silence. Wrong expectations produce false silence.

The spec's summary line - "blocker 2 shrinks to two boot-time reads where validation actually works" - is not supported.

---

## BLOCKER 3 - the applied-vs-data split makes "restart to apply" false for the key the ask is about

`subscribe:` is boot-only, verified. `managed_repos` is read by the director through `config show --json` per invocation, per the spec's own frozen prompt text. One file edit therefore takes effect at two different times, on two different clocks.

The offboard direction is the dangerous one. At T0 the operator removes repo X from both lists. Authority drops instantly (next `config show`); event routing persists until restart. Between T0 and restart the manager is still subscribed to X and `auto_dispatch` still arms `issue-lifecycle` on `github.issues.assigned` and `pr-closed` on `github.pull_request` closed, both `allow_self_authored: true` (`package/agent.yaml:101-110`, the spec's own citation). Workers launch on a repo the director now believes it does not manage, under a policy/prompt branch-delete guard (ruling 4) whose scope is defined by the list that already changed.

This is the spec's own "consequence 1" - *"the fleet would run half-migrated with no signal"* - reproduced on a different axis, in the revision that used that consequence to kill the previous design. And the spec asserts the opposite: the supervisor heartbeat is "the only place the two can disagree."

---

## BLOCKER 4 - step 4 is not implementable at the function it names, and breaks a cross-repo contract if forced

> `_scan_env_refs` (`bobi/config.py:190-197`) is a regex over raw file **text**, so it scans the overlay's text too and concatenates.

`_scan_env_refs(agent_yaml: Path)` receives a bare file path. It has no `project_path` and therefore **no way to locate the overlay**. Only `find_env_var_refs(project_path)` (`config.py:114`) has it.

Forcing it anyway hits two other callers: `scan_required_vars` (`config.py:211`) and `scan_declared_vars` (`config.py:223`), both documented for "a package file that isn't **installed** yet" (`:206`). They are a live cross-repo contract - `moda-labs/moda-agents` calls both at `bobi-deploy/bobi_deploy/src/bobi_deploy/deploy.py:543-544` and `scripts/check-deploy-compose.py:118`, and `scan_declared_vars` "doubles as the prune authority and the env-file filter" (`config.py:220`). Concatenating one runtime's overlay into a scan of *another team's* source `agent.yaml` corrupts that team's declared secret surface at deploy time.

The related premise is also false: "The overlay can never carry `build:` (it is not in the applied set)". Not being applied does not stop an operator writing `build:` into a free-form YAML file, and `_scan_env_refs` scans raw text with no key awareness. The safe direction survives (over-require), but the spec encodes an impossibility that is not one.

---

## MAJOR 5 - the flagship "blocker 1 falsified" test cannot distinguish the two designs

> Integration, the contract blocker 1 falsified: edit the overlay while a manager runs, assert the manager's live subscriptions do **not** change until restart ... this one fails against that design and passes against this one.

The spec's own text at "Three consequences follow" says that under the previous design `subscribe:` was *"read once at `service.py:534`"* and *"stayed on the old set."* Verified: `service.py:534` captures into a local and hands it to the spawn at `:664`. Live subscriptions do not change mid-process under **either** design, so this assertion passes against both. The test is vacuous as a falsifier.

To actually falsify the chokepoint design the assertion has to land on a `Config.load` consumer: `max_launch_depth` (`launch_lineage.py:244`), `entry_role` (`monitors/scheduler.py:419`), or `launch_admission` (`subagent.py:272`).

---

## MAJOR 6 - `config show` is a specified contract with no failure mode and no redaction statement

The frozen prompt parses `--json` to resolve `managed_repos`, i.e. write authority. `config show` is not on the boot path, so it never runs the validation. The spec does not say what it does with an unparseable overlay. If it degrades to pack-only, the director silently reads the pack's `managed_repos` (lightweave + moda-skills) and treats that as authority - a wrong-authority read with no signal.

It also "prints the effective document", which contains `services:` with `credentials:` (`package/agent.yaml:10-11, 16-18, 22-23`). The spec never states interpolated vs raw, or redaction, for a command whose output a prompt is told to parse.

---

## MAJOR 7 - `run/workspace/` is agent-writable, so the authority list becomes self-modifiable

Verified: `drwxr-xr-x bobi bobi /data/.bobi/agents/eng-team/run/workspace`, and agents run as `bobi`.

Ruling 3 is settled and I am not relitigating it. The gap is that the spec names only the fleet-ops consequence ("no drift detection and no audit trail") and never the shape change: before, adding a repo to `managed_repos` required a reviewed PR to `moda-labs/moda-agents` plus a reinstall; after, a single unreviewed agent turn writing one line grants it. The spec's guard for branch-delete safety is policy/prompt (ruling 4) - the same layer that would have to protect the file defining that policy's scope. Zach ruled the move; the spec owes an explicit statement of this consequence and whatever mitigation it accepts (even "accepted, no mitigation").

---

## MAJOR 8 - the reader table's misclassification propagates into the invariant test

The specified test asserts `OVERLAY_APPLIED_KEYS` contains no key read by `env.py:34`, `setup/actions.py:164`, `script_cache_checks.py:649` or `Config.load`. It omits `registry.py:80` -> `_read_records`, because the table calls that "not a content read" (WRONG, above).

Failure scenario: a later revision adds `monitors` to `OVERLAY_APPLIED_KEYS` - "one entry, not a new mechanism", as the spec invites. The specified test passes. `MonitorRegistry.load` (`service.py:541`) reads `monitors:` raw from the pack, `Config._parse` reads `monitors_raw` uninterpolated (`config.py:564`), and the overlay's monitors reach neither. That is precisely the divergence class `env.py:25-27` warns about, which the spec cites as the reason the test exists.

---

## MINOR

- **9.** Call-site count is **40**, not 41 (`cli.py:188`). The number is quoted as a correction of a prior revision, so it should be right.
- **10.** `--json` emits `"overlay-data"`; the human view marks `overlay (data)`. Two vocabularies for one tier in a contract a prompt parses.
- **11.** Review record says `setup/actions.py` moved "169 to 171"; the real line is `164`, which the spec's own table has correct.
- **12.** `for_agent` is optional ("if present"), but the pack-swap disaster it exists for is unguarded whenever it is absent - and the seed template "defines no keys", so a hand-written overlay has no guard. Make `for_agent` required whenever any applied key is present.
- **13.** Unstated operator consequence of the env-ref union: `_validate_or_raise` runs at `service.py:488`, right after `_load_config_or_raise` at `:484`. A one-line overlay edit referencing an unset `${VAR}` now hard-fails the whole manager boot, not just the subscription. Correct direction, but it belongs in the docs step alongside the other two named consequences.
- **14.** `doctor.py:244` / `config.py:114-141` / `drain.py` count drift, above.

---

## Verdict on the four prior blockers

- **B1 (uncached `Config.load`)**: genuinely **closed for `subscribe:`** - the 40 `Config.load` sites never see the overlay. But reopened at a different seam for `managed_repos` (BLOCKER 3), which is the half of the ask the ruling actually moved.
- **B2 (torn reads)**: **relocated and worsened** (BLOCKER 2). Three non-boot read paths remain, and the failure mode changed from "raises into a wrong default" to "silently returns a plausible wrong list".
- **B3 (raw-reader inventory)**: **not closed**. Grep 1's coverage is now complete, but one row is misclassified, grep 2 was counted rather than classified, and the count is wrong.
- **B4 (`Config._parse` interaction)**: **genuinely closed**. No `Config` interaction remains. The parallel hazard survives only through `registry.py`/`_read_records`, which is MAJOR 8, not B4.

Two of four closed. The reversal is the right architectural call and the spec is markedly more honest than its predecessors, but it did not re-derive the fallback semantics of the one code path it now depends on entirely.

**NOT IMPLEMENTABLE.**
