# Review round 3: revision 3 of the workspace-event-subscriptions spec

Spec reviewed: `plans/2026-10-09-workspace-event-subscriptions.md` at `5b3e8740` (revision 3, 549 lines).
Code pins: bobi-agent `83bebe49`, moda-agents `bf9d1088`.
Run 2026-10-09 as two independent fresh-context reviewers, both blocking, both barred from the network and from any write.

Scope: the five revision-3 pieces that no prior round reviewed (the separate per-session accepted-set file, conditional seeding, the `UnauthorizedTopics` contract on `put_subscriptions`, `doctor` as a fourth reader, the `ensure_running` ordering guard), plus interaction bugs between them, plus a cruft pass over the whole spec.

No cross-model leg. `codex` is 401 and `aichat` is unconfigured in this container, so both legs are the same model with fresh context and required evidence. This is not claimed as a second model.

Both reports are reproduced verbatim below.

---

## Leg A: correctness of the revision-3 deltas

# Round 3 review: correctness (revision 3 deltas)

## Verdict

Revision 3 is close but not implementable as written: one line in the test-plan fold is unsatisfiable against the real fixtures, and three of the five new pieces carry a stated property that the spec's own adjacent text contradicts.
The worst problem is H1: the spec tells the implementer to make `tests/test_event_subscription.py:469-471` and `:514-515` "additionally assert the new accepted-set file now exists", but both of those tests answer the PUT with a bare `{"ok": True}`, and the spec's own G5 fold says an absent `subscriptions` member is NOT recorded, so the file will not exist and the assertion cannot pass.
The citation work is otherwise the strongest of the three revisions: 100+ tokens checked, 3 defects, all cosmetic.
P1's session identity and `inbox/` survival both check out under execution, so the F1 trap that killed revision 1 is genuinely fixed.

## Citation audit

Every `path:line` token on a rev-3 added line, checked at `83bebe49` (bobi-agent) and `bf9d1088` (moda-agents).

### WRONG or misdescribed (3)

| Citation | Status |
|---|---|
| `bobi/events/server.py:838-842` | WRONG RANGE. `:838-839` is `except Exception: data = {}`. The `400 unauthorized_topics` block is `:840-842`. |
| `bobi/doctor.py:669-678` | MISDESCRIBED. The spec says the range "catches every exception into `CheckResult(ok=True, detail=f"skipped: {exc}")`". It has TWO arms: `:673-675` is `except FileNotFoundError` returning `detail="no agent config"`; only `:676-678` is the `skipped:` arm. |
| `tests/test_event_subscription.py:514-515` | MISATTRIBUTED. The spec calls `:469-471` and `:514-515` "their 'the saved deployment survives' docstrings". That text is a *comment* at `:469` only. `:514-515` sits in `test_authorization_raising_still_puts_the_raw_list`, whose docstring is "When pre-PUT authorization RAISES, send the raw subscribe list anyway." |

### Accurate in substance, imprecise in form (2)

| Citation | Note |
|---|---|
| `bobi/subagent.py:1805-1808` | The spec says the register path uses `authorize_resources` "with `filter_unauthorized=True`". The call site passes no such kwarg; the value comes from the default at `server.py:682`. Verified by execution: `filter_unauthorized default = True`. |
| `bobi/subagent.py:1859-1864` | The quote the spec attributes to both this site and `server.py:706-712` is verbatim only in `server.py:708-710`. The subagent docstring paraphrases: "dropping the topic would silently unsubscribe a valid deployment." |

### VERIFIED (all remaining)

bobi-agent: `AGENTS.md:12`, `:97`, `:133`; `bobi/doctor.py:51`; `bobi/events/client.py:260`, `:294-297`, `:501-502`, `:518-519`; `bobi/events/protocol.py:55-70`; `bobi/events/server.py:627`, `:640`, `:650-655`, `:680-684`, `:706-712`, `:713-714`, `:761-765`, `:781-784`, `:849-875`; `bobi/events/state.py:21-25`, `:32`, `:32-64`, `:37`, `:44-52`, `:55-64`, `:79`; `bobi/events/subscriptions.py:13`, `:25`, `:28`, `:41-42`, `:43-48`, `:46-48`, `:47`, `:51`, `:59-66`, `:60`, `:61`, `:63-66`, `:68-78`; `bobi/events/adapters.py:139-142`, `:290-315`; `bobi/fsutil.py:100`, `:162`; `bobi/http.py:78`; `bobi/ingress.py:83`; `bobi/install.py:174-192`; `bobi/paths.py:92`; `bobi/prompts/resolver.py:86-97`; `bobi/runtime_guard.py:143-153`; `bobi/service.py:135-144`, `:184-195`, `:625`, `:625-642`, `:759`; `bobi/session.py:1657-1660`, `:1673`, `:1710`; `bobi/state_version.py:25-27`; `bobi/subagent.py:1715`, `:1765`, `:1767`, `:1812`, `:1834`, `:1834-1835`, `:1835`, `:1838`, `:1867`, `:1883`, `:1891`, `:1891-1892`, `:1892`, `:1894-1924`, `:1917-1920`, `:1926-1946`, `:1938-1946`, `:1948-1955`, `:1974-1982`, `:1982`, `:1990`; `bobi/supervisor/snapshot.py:104-109`; `bobi/cli.py:80-95`, `:2965`, `:2992`, `:3016`, `:3155`, `:4168`, `:4177-4179`; `docs/BUILDING_AGENT_TEAMS.md:129-130`, `:129-133`; `docs/EVENT_SERVER.md:378-398`, `:396-398`; `pyproject.toml` (`pyyaml>=6.0`, no other YAML lib); `core.ts:411`, `:445-450`, `:1461`, `:1473-1480`, `:1498-1500`, `:1504-1512`, `:1508-1512`, `:1509`, `:1522-1530`, `:1535`, `:1537-1540`; `worker/src/index.ts:147-165`; `core.spec.ts:2030`, `:2284-2306`, `:2314-2324`; `tests/integration/test_event_server.py:62`, `:371`, `:394`, `:510-520`; `tests/integration/test_e2e_event_flow.py:87`; `tests/integration/test_event_isolation.py:1-16`, `:211`; `tests/test_event_client_heartbeat.py:144-165`; `tests/test_event_subscription.py:71`, `:110-136`, `:469-471`; `tests/test_ingress.py:260-276`, `:306-324`; `tests/test_symmetric_node.py:90-102`; `tests/test_service.py:21-26`.

moda-agents: `agents/moda-eng-team/agent.yaml:103-126`, `:124-126`, `:127-134`, `:128-134`, `:151`, `:155`, EOF at `:162`; `agents/baohua/agent.yaml:105`, `:118`; `agents/baohua/README.md:152`; `scripts/check-deploy-compose.py:76-83`, `:85-94`; `roles/director/ROLE.md:12`, `:22`; `.github/fleet-version` (`BOBI_VERSION=0.59.0`); `deploy-agent-teams.yml:91`, `:177`; `team-images.yml:139`; `version-gate.yml:100`; `release-fleet.yml:342`.

`core.ts:1522-1530` in particular is the correct fold of round 2's G13. `tests/test_event_client_heartbeat.py:144-165` is a test, not a "fixture", but the harness the spec actually relies on for tests 8-10 is `tests/test_event_subscription.py:110-136`, which is correct and which does drive `on_deaf_reconnect` with no 95s wait. Round 2's G12 is properly fixed.

### Rev-3 derived claims, re-derived independently

- Pack census (5 of 7 with no `subscribe:`): VERIFIED, and `grep -c '^subscribe:'` does not miss a nested key. A substring grep over all seven files finds `subscribe` as a top-level key only in `baohua` (`:160`) and `moda-eng-team` (`:127`); the other five have no `subscribe` substring at all except in prose.
- "Four of those five have `events: true` services": VERIFIED. gtm-team 1, market-research 2, roadmap-pm 1, support-manager 1, zachs-personal-assistant 0.
- "bobi-agent ships no `agent.yaml` template with `subscribe:`": VERIFIED. `grep -rln '^subscribe:'` over `agents/`, `tests/fixtures/`, `examples/` returns nothing.
- "Both manager-booting test fixtures are in that state": VERIFIED. `tests/integration/conftest.py:96-107` writes an `agent.yaml` dict with `services: [{name: github, events: True}]` and no `subscribe` key.
- The seed body equals `agents/moda-eng-team/agent.yaml:128-134`: VERIFIED against the LIVE composed pack. `/data/.bobi/agents/eng-team/run/package/agent.yaml:120-127` holds exactly those seven topics, and the base pack `agents/eng-team/agent.yaml` (bobi-agent, v1.5.6) declares no `subscribe:`, so composition adds nothing.
- "nine existing tests answer the PUT with a bare `{"ok": True}`": VERIFIED. Nine catch-all handlers in `tests/test_event_subscription.py` at `:94`, `:119`, `:214`, `:348`, `:416`, `:456`, `:499`, `:534`, `:562`.
- The `_detect_github` probe: REPRODUCES exactly, with `__file__` proving the right tree.
- `core.spec.ts` asserts `unauthorized_topics` only on the register path: VERIFIED, `grep -n 'unauthorized_topics'` returns exactly one line (`:2030`).

### Positive verifications on the unreviewed pieces

- **P1 x `inbox/` (the F1 trap).** FIXED. `session.py:1657-1660` prepends `inbox/<name>` into `keys`, `keys` is the `subscribe` argument at `session.py:1673`, `active_subscriptions = list(subscribe)` at `subagent.py:1765`, and the record site at `:1835` sees the post-prepend list. `authorize_resources` passes `inbox/` through in both modes: `inbox/<name>` has no colon, so `service == ""`, which misses the gate at `server.py:742`, misses `:751`, and lands on `kept.append(sub)` at `:761-764`. Pinned by an existing test: `tests/test_event_subscription.py:130` asserts `mock_register.call_args.args[2] == ["inbox/self"]` after `github:o/r` was filtered out.
- **P1 x session identity.** CORRECT. `service.py:708` sets `session_name = manager_session_name(project_path, role)` and `:756` passes it as the Session name; `role` comes only from `cfg.entry_role` (`:621`), with no CLI override on the boot path, which is the same value `manager_session_name(project_path)` resolves with `role=None` (`:137-143`). Measured on this deployment: `manager_session_name = bobi-eng-team-director`, which `_safe_session` leaves unchanged.
- **P3 x the classifier and the deaf hook.** CORRECT in all four respects, verified by execution: `UnauthorizedTopics` mro is `['UnauthorizedTopics', 'Exception', ...]`, `issubclass(UnauthorizedTopics, EventProtocolError)` is `False`, its ctor is `(self, topics: list[str])` so the PUT path can construct it exactly as `:842` does, and `raise_for_protocol_error(400, {'error':'unauthorized_topics'})` passes through with no raise. So the spec's stated order (`raise_for_protocol_error` then the `UnauthorizedTopics` raise) does not dead-code the raise even though it inverts the register path's order; the raise reaches `_sync_saved_deployment`'s `except Exception` at `:1896` and would land in the `else` arm, which is exactly why the new classifier branch is required; and on a deaf reconnect it reaches `client.py:518-519`, so the `debug` -> `warning` raise is the line that fires.
- **P1 x the register path's recorded list.** The record is the list `register()` was given: `_post_register` puts `subscriptions` into the body verbatim (`server.py:829`) and `core.ts:1461` stores it with no dedup or normalization. Monitor and lifecycle keys survive `authorize_resources` for the same reason `inbox/` does (no colon).
- **P1 x no torn read.** `atomic_write_text` is tmp + `os.replace` (`fsutil.py:126-133`), so the spec's "old or new, never partial" claim holds.
- **P2 x upgrade, interpolation.** No fleet pack uses `${}` inside its `subscribe:` list, verified by an awk over each `subscribe:` block in all seven packs (zero hits). The hazard in H7 is latent, not live.
- **The upgrade-boundary restart.** Correctly handled. The record is written only at session start, so every `add` is persist-only until the manager restarts once; the spec names this case ("a deployment record with no accepted-set file") and test 18 covers it.

## Findings

### H1. "`:469-471` and `:514-515` additionally assert the new accepted-set file now exists" is unsatisfiable, and contradicts the spec's own absent-vs-empty rule [BLOCKER]

**Claim:** Both of those tests answer every request, PUT included, with a bare `{"ok": True}`, so `put_subscriptions` returns `None`, the spec's own rule says nothing is recorded, and the accepted-set file does not exist for the assertion to find.

**Evidence:**
```
$ sed -n '452,471p' main-verify-1116/tests/test_event_subscription.py
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.method == "POST" and str(request.url).endswith("/resources/authorize"):
            return httpx.Response(403, json={"error": "forbidden"})
        return httpx.Response(200, json={"ok": True})      # <- line 456, the PUT reply
...
    # The saved deployment survives — nothing was re-minted.
    assert json.loads(state.read_text()) == {
        "deployment_id": "dep-L", "api_key": "key-L"}      # <- lines 469-471

$ sed -n '499p' main-verify-1116/tests/test_event_subscription.py
        return httpx.Response(200, json={"ok": True})      # the :514-515 test's PUT reply
```
The spec, two paragraphs apart:
```
"It returns the body's `subscriptions` array, or `None` when the body omits it."
"An absent or empty result is NOT recorded..."
"`:469-471` and `:514-515` additionally assert the new accepted-set file now exists"
```

**Why it matters:** An implementer follows the change list, adds `assert accepted_subscriptions_path(...).exists()` to both tests, and both fail. The only ways out are to change the mock PUT replies to include a `subscriptions` array, which destroys the thing the `{"ok": True}` shape exists to prove (that the client tolerates a minimal legacy reply, the exact property round 2's G5 was about), or to delete the new assertion, which leaves the two write sites with no test at all. This is also a mis-fold: round 2's G7 asked for these sites to "additionally pin that `deployment_id` and `api_key` are UNCHANGED".

**Suggested fold:** Replace that sentence with G7's actual ask. Say `:469-471` and `:514-515` stay exact-equality asserts on the credential record, which already proves the new write never touches it, and that the accepted-set file's existence is proved by a NEW test whose mock PUT returns `{"subscriptions": [...]}`. Add that new test as test 21 and pair it with test 10 (the omitted-`subscriptions` case).

### H2. `list` "flags any difference" is a structural false positive: on this deployment it would flag 17 benign topics on every run [MAJOR]

**Claim:** The workspace file deliberately excludes `inbox/<session>`, monitor keys and lifecycle keys, while the recorded accepted set necessarily contains them, so a plain difference flag fires on every `list` for every team, forever.

**Evidence:** Measured on the live root at `83bebe49`:
```
$ PYTHONPATH=main-verify-1116 python -c "...compose the real sets for /data/.bobi/agents/eng-team/run..."
workspace/pack subscribe layer (7): ['github:moda-labs/lightweave', ..., 'linear:MOD']
monitor keys (12): ['memory.updated', 'system/memory.updated', 'pr.conflict_detected', ...]
lifecycle keys (4): ['session.completed', 'agent/session.completed', 'session.failed', 'agent/session.failed']
manager_session_name = bobi-eng-team-director
LIVE accepted set size = 24
workspace-file size    = 7
entries in accepted set but NOT in the workspace file:
    inbox/bobi-eng-team-director
    memory.updated
    system/memory.updated
    ... (17 total)
```
The spec says both halves itself: "Monitor topics, lifecycle topics and `inbox/<session>` are NOT in this file" and "`list` prints the persisted workspace list and the recorded accepted set with its timestamp, and flags any difference."

**Why it matters:** D4 asks for "topics the server silently dropped", which is one direction only: persisted but not live. As specified, `list` reports a 17-entry difference on a perfectly healthy team, so the signal is noise and the operator learns to ignore it. The one real signal the spec names (an `add` that lost the boot race, so the file is ahead of the server) is buried in it.

**Suggested fold:** Scope the comparison to one direction. "`list` flags every topic in the persisted list that is absent from the recorded accepted set, and prints the accepted set's extra topics as a separate derived group (inbox, monitor, lifecycle), which are expected and never flagged."

### H3. Step 2 does live network work before step 3's bail-out, so P5's stated guarantee is false, and it does it holding a blocking cross-process lock the manager's boot also takes [MAJOR]

**Claim:** Step 3 claims "Resolving this first is what keeps a persist-only run from doing any network or process work", but step 2 runs first and the spec itself says step 2 can make live Slack and Linear API calls.

**Evidence:** The spec's own two steps, in order:
```
2. Under `file_lock`, load `workspace/subscriptions.yaml`, apply the topic edit, ...
   When the file is absent, seed it from `discover_subscriptions(project_path)` ...
   That read can make live Slack and Linear API calls on such a team, once, ...
3. Resolve the manager's session and its live identity ...
   Resolving this first is what keeps a persist-only run from doing any network or process work.
```
`discover_subscriptions` really does call out:
```
$ sed -n '298,306p' main-verify-1116/bobi/events/adapters.py
        resp = pooled.post(
            "https://api.linear.app/graphql",
            json={"query": "{ teams { nodes { key } } }"},
            ...
            timeout=5.0,
        )
```
And the lock is blocking with no timeout and no `LOCK_NB`:
```
$ sed -n '174,180p' main-verify-1116/bobi/fsutil.py
    import fcntl
    path = Path(path)
    lock_path = path.with_name(f"{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
```
The spec puts seeding on the same lock: "Seeding takes the same lock, so an `add` racing a cold start cannot be lost."

**Why it matters:** Two concrete effects on a fileless team. First, `bobi agent <name> subscriptions add X` on a STOPPED team, the exact persist-only case step 3 exists for, POSTs to api.linear.app and Slack `auth.test`/`conversations.list` before discovering there is nothing to apply. Second, a cold manager start that reaches `seed_workspace_subscriptions` while the CLI holds that lock blocks for the duration of those calls, and `bobi/session.py:1668-1672` records that the repo already treats a tens-of-seconds boot stall as tripping liveness probes. ("The CLI aborts on a Slack transport error" is NOT a risk here: `_detect_slack` absorbs everything at `adapters.py:203-204` and `_detect_linear` at `:313-314`.)

**Suggested fold:** Reorder, and correct the claim. Make the session/record/accepted-set resolution step 1, then persist, then apply. If a fileless `add` must still seed from the effective set, say the `discover_subscriptions` call happens OUTSIDE `file_lock` and that the lock is reacquired only for load-mutate-save. Delete "any network or process work" and replace it with the accurate and still-useful property: "no `ensure_running`, no `authorize_resources`, and no PUT".

### H4. No lock across steps 3-8, so an `add` interleaved with a manager restart reverts the boot's recomposed set [MAJOR]

**Claim:** Step 3 reads the accepted set and step 7 PUTs a list derived from it, with nothing holding between them. A manager restart in that window writes a fresh set to the server, and the CLI's later PUT replaces it with the pre-boot snapshot plus one topic.

**Evidence:** `{"replace": [...]}` is total, not additive:
```
$ sed -n '1517,1536p' main-verify-1116/event-server/core/src/core.ts
	if (replaceSubs !== undefined) {
		const desired = [...new Set(replaceSubs)];
		...
		for (const sub of deployment.subscriptions) {
			if (!desiredSet.has(sub)) { await storage.removeSubscription(...); removed++; }
		}
		...
		deployment.subscriptions = desired;
```
The boot PUT recomposes from mutable state, including the monitor set:
```
$ sed -n '625,642p' main-verify-1116/bobi/service.py
    subscribe = discover_subscriptions(project_path)
    subscribe += [s for s in (extra_subscribe or []) if s not in subscribe]
    ...
    for key in monitor_subscription_keys(monitor_events):
        if key not in subscribe: subscribe.append(key)
    for key in lifecycle_subscription_keys():
        if key not in subscribe: subscribe.append(key)
```
`bobi/cli.py:2965`, `:2992` and `:3016` write `package/monitors.yaml` at runtime and never re-PUT, so the monitor set can legitimately change between the CLI's step-3 read and the boot's PUT.

**Why it matters:** Concrete sequence. The record holds `[inbox/M, github:a, monitor/old, ...]`. The operator runs `monitors add newmon`, which writes `monitors.yaml` and does not PUT. The operator runs `subscriptions add github:b`, which reads the record at step 3. The manager restarts and `_sync_saved_deployment` PUTs the recomposed set, now including `monitor/new`, and records it. The CLI's step 7 then PUTs `record + github:b`, which omits `monitor/new`, and `core.ts:1522-1530` removes it from the index. `monitor/new` findings stop being delivered with no error anywhere. That is precisely the trap the design names "the largest trap" and that test 4 exists to guard, arriving from the other direction. Revision 3's justification for taking no lock on this file ("a concurrent write reads as either the old or the new document and never a partial one") answers torn reads, which is correct, but not the read-A/read-A/PUT-1/PUT-2 interleave round 2's G2 raised in its second paragraph, so that half of G2 is folded incompletely.

**Suggested fold:** Two sentences. Hold `file_lock(accepted_subscriptions_path(project_path, session))` across steps 3 through 8, and have both manager write sites take the same lock. Re-read the record inside the lock immediately before building the PUT, and abort with a "the manager restarted, re-run the command" message if it changed since step 3. Add that case to test 16.

### H5. P4 is not implementable: nothing names the exception `doctor` must discriminate, and the spec describes the wrong arm [MAJOR]

**Claim:** `doctor.py` must tell "malformed workspace subscriptions file" apart from every other exception to return `ok=False`, but the spec never names an exception type for `workspace_subscriptions`'s raise, and `bobi/events/subscriptions.py` gains no such class in the change list.

**Evidence:** The real shape of the arm, which has two clauses, not one:
```
$ sed -n '663,684p' main-verify-1116/bobi/doctor.py
def _check_ingress_reachability() -> CheckResult:
    root = bound_root()
    if not root:
        return CheckResult("Ingress reachability", ok=True, detail="no runtime selected")
    try:
        from bobi.ingress import check_ingress_reachability
        warning = check_ingress_reachability(root)
    except FileNotFoundError:
        return CheckResult("Ingress reachability", ok=True, detail="no agent config")
    except Exception as exc:
        return CheckResult("Ingress reachability", ok=True, detail=f"skipped: {exc}")
```
All the spec says about the raise: "A malformed file raises, and the exception carries the file path." The change list for `bobi/events/subscriptions.py` adds `workspace_subscriptions`, `seed_workspace_subscriptions` and two edits, and no error class. The existing pack parser raises a bare `yaml.YAMLError`, pinned at `tests/test_ingress.py:302`.

**Why it matters:** With no named class the implementer has two bad options: catch `yaml.YAMLError` in `doctor.py`, which cannot distinguish a malformed WORKSPACE file from a malformed pack `agent.yaml` and would flip the `agent.yaml` case from `ok=True` to `ok=False` as a side effect, or string-match the message. Test 12 asserts "`bobi doctor`'s ingress check reports `ok=False` with the path", so the test cannot be written either. Separately, the spec's description of `:669-678` omits the `FileNotFoundError` arm, so an implementer reading only the spec would not know that a missing config must stay `ok=True`.

**Suggested fold:** Name the class. "`workspace_subscriptions` raises `WorkspaceSubscriptionsError(path, cause)`, a new class in `bobi/events/subscriptions.py`, and `bobi/doctor.py` adds an `except WorkspaceSubscriptionsError` arm BEFORE the existing `except FileNotFoundError` and `except Exception` arms, returning `ok=False` with the path. The two existing arms are unchanged."

### H6. After rev 3's own change, `explicit_subscriptions` is no longer a pack reader, so the seeder has no function to read "the pack's list" [MINOR]

**Claim:** The spec makes `explicit_subscriptions` return the workspace list when present, then asks `seed_workspace_subscriptions` to gate on "the pack's list is non-empty", and adds no pack-only reader to do it.

**Evidence:** The change list for `bobi/events/subscriptions.py`, in full, is: add `workspace_subscriptions`, add `seed_workspace_subscriptions`, "Have `explicit_subscriptions` (`:25`) return the workspace list when it is not `None`", "Have `discover_subscriptions` (`:51`) consult `workspace_subscriptions` before and outside the `try`", "Leave the pack gate at `:61` unchanged." No third reader. The pack read today is `:40-48`, inside `explicit_subscriptions`.

**Why it matters:** It happens to work, because the seeder only runs when the file is absent, in which case `explicit_subscriptions` falls through to the pack. But the correctness depends on an unstated ordering: the absence check must dominate the pack read. An implementer who reads the gate in the order the spec writes it ("write-if-absent AND only when the pack's list is non-empty") and evaluates the second clause first on a path where the file DOES exist is measuring the workspace list while believing it measures the pack. Test 15 ("a second boot does not overwrite an edited file") would still pass, so nothing catches it.

**Suggested fold:** Name the private helper. "Factor the pack read at `:40-48` into `_pack_subscriptions(project_path)`; `explicit_subscriptions` calls it after the workspace layer, and `seed_workspace_subscriptions` calls it directly so its gate can never read the file it is about to write."

### H7. The seeder's written form (raw vs interpolated) is unspecified, and the interpolated form silently decouples the live set from the env [MINOR]

**Claim:** `workspace_subscriptions` reuses "the env interpolation `explicit_subscriptions` applies at `:46-48`", and the seeder writes "that same list", but nothing says whether the file gets the raw `${VAR}` text or the resolved value.

**Evidence:** An unset variable resolves to the empty string and is then filtered out:
```
$ sed -n '238,252p' main-verify-1116/bobi/config.py
def _interpolate_env(value, env=None):
    """... An unset (or empty) VAR resolves to its ``:-`` fallback
    when it has one, else ""."""
    ...
        def _resolve(m): ref = parse_env_ref(m.group(1)); return lookup.get(ref.name) or ref.default
```
Pinned behavior at `tests/test_symmetric_node.py:90-102`: a pack whose `subscribe:` is `[${MISSING_TOPIC}, ${OPTIONAL_TOPIC:-}, 123]` discovers as `[tmp_path.name]`.

**Why it matters:** If the seeder writes the interpolated form, a pack entry whose variable was unset at seed time is written as nothing and can never come back, and one that resolved is frozen at that value. Today the pack is re-interpolated on every boot, so a credential or channel id supplied later takes effect at the next start. That is a behavior change the Migration section's "No behavior change on upgrade" does not cover. The exposure is latent only: no fleet pack uses `${}` inside `subscribe:` today, verified.

**Suggested fold:** One sentence in the seeding section. State that the seeder writes the RAW pack value and that `workspace_subscriptions` interpolates on read, so the env coupling is preserved, OR state that it writes the interpolated form and that env coupling is deliberately dropped at seed time. Add the chosen behavior to test 15.

### H8. Tests 10 and 20 do not meet the spec's own red-before bar [MINOR]

**Claim:** The spec applies its vacuity rule only to test 11, and tests 10 and 20 fail it.

**Evidence:** The spec's bar, stated under test 11: "A version that passes before the gate changes is vacuous and does not count."
Test 10: "A PUT whose 200 body omits `subscriptions` leaves the record untouched, and the next deaf reconnect still replays a non-empty set rather than `{"replace": []}`." The second clause passes pre-change: `_resubscribe_on_deaf` replays `active_subscriptions` (`subagent.py:1982`), which is non-empty, and `tests/test_event_subscription.py:131-136` already asserts exactly one non-empty `{"replace": [...]}` after `on_deaf()`.
Test 20: "the first boot's composed list is byte-identical to the pre-upgrade composed list." No single test process holds both code versions, and the only expressible form compares `discover_subscriptions` before and after calling `seed_workspace_subscriptions`, a function that does not exist pre-change.

**Why it matters:** Test 10's only non-vacuous half needs the new file, so as worded it can be written to pass trivially. Test 20 as worded is not buildable at all, and the implementable rewrite does not test the migration property the Migration section rests on.

**Suggested fold:** Reword test 10 to assert the record's PREVIOUS value is intact after an omitting PUT (which requires two PUTs, the first returning `subscriptions`), and keep the `{"replace": []}` clause as the consequence. Reword test 20 as "`discover_subscriptions` returns the same list before `seed_workspace_subscriptions` runs and after it has written the file, for a pack that declares `subscribe:`", and say plainly that the cross-version comparison is covered by the canary, not by a unit test.

### H9. Step 5's input list is unspecified, so an `add` may re-POST the real credential once per existing global topic [MINOR]

**Claim:** "For `add` only, call `authorize_resources(..., filter_unauthorized=False)`" never says which list is passed.

**Evidence:** `authorize_resources` POSTs the live credential per global topic in whatever list it is given:
```
$ sed -n '740,745p;766,784p' main-verify-1116/bobi/events/server.py
    for sub in subscribe:
        service = sub.split(":", 1)[0] if ":" in sub else ""
...
        resource = sub.split(":", 1)[1]
        cfg_service, cred_key = _RESOURCE_CRED_KEYS[service]
        credential = cfg.credential(cfg_service, cred_key)
...
            granted = _authorize_one_resource(
                base_url, service, resource, credential, bubble_id, bubble_key,
            )
```
On this deployment the step-4 list carries six global topics (4 `github:`, 2 `linear:`) plus the newly added one.

**Why it matters:** Passing step 4's full list means seven signed POSTs carrying the real GitHub token and Linear API key on every `add`, where one would do. It is not incorrect (with `filter_unauthorized=False` the return equals the input), just needlessly chatty on a path whose own section is titled "Authorization is a network write".

**Suggested fold:** "Step 5 passes only the newly added topics, not the whole PUT list, and its return value is discarded; `filter_unauthorized=False` is kept so the call can never narrow the set."

### H10. `bobi/events/server.py:838-842` starts two lines early [NIT]

**Claim:** The `400 unauthorized_topics` parse the spec points at is `:840-842`.

**Evidence:**
```
$ sed -n '836,843p' main-verify-1116/bobi/events/server.py
	try:
		data = resp.json()
	except Exception:
		data = {}
	if resp.status_code == 400:
		if isinstance(data, dict) and data.get("error") == "unauthorized_topics":
			raise UnauthorizedTopics(list(data.get("topics") or []))
	raise_for_protocol_error(resp.status_code, data)
```

**Why it matters:** The spec cites this range twice as the pattern the new raise must match, and `:838-839` is the json-parse fallback, not the pattern. Minor, but the register path puts the raise BEFORE `raise_for_protocol_error` while the spec's `put_subscriptions` puts it after; an implementer who reads the two lines of context the citation actually covers sees neither.

**Suggested fold:** Change both occurrences to `:840-842`, and add "(note the register path raises before `raise_for_protocol_error`; either order works here because that function only raises for `invalid_protocol` / `incompatible_protocol` / 426)".

### H11. `:514-515`'s docstring attribution is wrong [NIT]

**Claim:** "their 'the saved deployment survives' docstrings" describes a comment at `:469` and does not apply to `:514-515` at all.

**Evidence:**
```
$ sed -n '469,471p' main-verify-1116/tests/test_event_subscription.py
    # The saved deployment survives — nothing was re-minted.
    assert json.loads(state.read_text()) == {
        "deployment_id": "dep-L", "api_key": "key-L"}
$ sed -n '477,479p' main-verify-1116/tests/test_event_subscription.py
def test_authorization_raising_still_puts_the_raw_list(
        mock_register, mock_client, _drain, local_project, _stub_ensure_running):
    """When pre-PUT authorization RAISES, send the raw subscribe list anyway.
```

**Suggested fold:** Drop the clause; H1's rewrite removes the sentence anyway.

### H12. The accepted-set path omits `_safe_session` [NIT]

**Claim:** The spec writes `<run>/state/subscriptions/<session>.json`, while the two files it says it follows both sanitize the session name.

**Evidence:**
```
$ sed -n '28,41p' main-verify-1116/bobi/events/state.py
def _safe_session(session: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", session) or "_"

def deployment_state_path(project_path: Path, session: str) -> Path:
    return (paths.state_path(project_path) / "deployments" / f"{_safe_session(session)}.json")

def session_cursor_path(project_path: Path, session: str) -> Path:
    ...
    return (paths.state_path(project_path) / "cursors" / f"{_safe_session(session)}.json")
```
Harmless for the manager (`bobi-eng-team-director` is already safe), but worker session names are caller-chosen (`subagent.py:836-838`: "``name`` is the session name and is caller-chosen, always").

**Suggested fold:** Write the path as `<run>/state/subscriptions/<_safe_session(session)>.json`.

## Sections I checked and found correct

- "The accepted set needs its own per-session file, not the credential record": every claim verified, including that `save_deployment_state` replaces the whole document with no lock and no read-first, and that three tests assert its exact shape.
- "The empty-list fallthrough is a real hazard": verified end to end, including the reproduced `_detect_github` probe and the `tests/test_ingress.py:306-324` pin that forces `discover_subscriptions` to keep calling `explicit_subscriptions` inside the `try`.
- "The server rejects; it does not silently drop": verified, including that `grep -n 'unauthorized_topics' core.spec.ts` returns exactly one hit, which is test 1's whole justification.
- "Authorization is a network write, it fails open, and it covers two services": verified, including the five server-side prefixes, the slack-grant-on-team-id reduction, and the no-`expirationTtl` write.
- Release ordering: every workflow citation and the `BOBI_VERSION=0.59.0` pin verified. Round 1's F4 is properly fixed.
- The moda-agents companion PR: all citations verified, including that `managed_repos:` really does run `:151` to EOF at `:162` and that `ROLE.md:12`/`:22` really never name the key.
- Tests 8 and 9 are buildable and red for the right reason on the `tests/test_event_subscription.py:110-136` harness, with no 95s wait. Round 2's G12 is fixed.
- Test 16 is buildable: `fcntl.flock` is per open file description, so two threads in one process each opening `<name>.lock` do serialize.

## NOT VERIFIED

- Whether the live event server currently holds the 24-topic set for `bobi-eng-team-director`. Reading it needs an authenticated call to the event server, which is a network operation I did not perform. The 24-entry set in H2 is COMPOSED from the real root's config, monitors and session name, not read from the server.
- Whether tests 2 through 7 pass against the real local event server. They look buildable: `tests/integration/conftest.py:151` and `:381` put `BOBI_ES_TEST_GRANTS_SECRET` in both the manager env and the `cli_run` env, so `_seed_test_resource_grant` (`server.py:745`) backs every global topic, and `cli_run` routes through `bobi agent <name> ...` (`conftest.py:372-373`) so `_detect_project_root` gets the bound root. I did not run the suite.
- Whether `require_app_identity` and `_resolve_channel_names` paginate, so I cannot put a number on how long H3's lock is held.

---

## Leg B: cruft and simplification over the whole spec

# Round 3 review: cruft and simplification

## Verdict

About 100 lines can come out (549 -> ~449), and one DESIGN element should be cut, not just prose: the boot-time `seed_workspace_subscriptions` step in `bobi/service.py`.
It is a second code path for a thing the CLI already does (step 2's fileless seed), it produces byte-identical content to that path on every pack it can fire on, it saves zero network calls, and it is the sole reason the spec carries a 23-line justification section (119-141), a 15-line design section (266-280), a `bobi/service.py` change, the pack-non-empty gate, and tests 15/16b/20.
It is also the root cause of round 2's blocker G1.
Cutting it makes the design summary at 199-200 literally true ("no change to `bobi/service.py`") instead of qualified.
The other two design elements round 1 and 2 argued about survive: the per-session accepted-set file is load-bearing for a reason neither prior round stated, and the `put_subscriptions` extraction pays for itself.
The test plan goes 20 -> 13 items. Q1 is not an open question; D4 already answers it.

## Proposed cuts, worst offender first

### K1. The boot-time seeder and everything that exists to justify it [CUT]

**Spec lines:** 119-141, 266-280, 361, 366-368, 472, 477, 483, 489-494. 46 lines.

**Claim:** Deleting `seed_workspace_subscriptions` changes no boot behavior, produces an identical workspace file on first `add`, and deletes two design sections, one change site, one justification section, and three tests.

**Evidence:**

With no workspace file and a pack list, `discover_subscriptions` returns the pack list and never reaches the auto-detect branch. `detect` was stubbed to raise; it was not called.

```
$ T=$(mktemp -d); mkdir -p $T/package; cat > $T/package/agent.yaml <<'EOF'
agent: test-agent
entry_point: director
subscribe:
  - github:moda-labs/lightweave
  - linear:MDS
services:
  - name: github
    events: true
EOF
$ PYTHONPATH=$BA_MAIN python3 - <<'PY'
import bobi.events.subscriptions as s, bobi.events.adapters as a
from pathlib import Path
print("module file:", s.__file__)
def boom(*a_, **k): raise AssertionError("detect() reached - network branch taken")
a.detect = s.detect = boom
print("explicit_subscriptions:", s.explicit_subscriptions(Path("$T")))
print("discover_subscriptions (no workspace file):", s.discover_subscriptions(Path("$T")))
PY
module file: /data/.bobi/agents/eng-team/run/worktrees/main-verify-1116/bobi/events/subscriptions.py
explicit_subscriptions: ['github:moda-labs/lightweave', 'linear:MDS']
discover_subscriptions (no workspace file): ['github:moda-labs/lightweave', 'linear:MDS']
```

Three consequences, each one of the seeder's stated reasons for existing.

1. No-file boot is unchanged. The spec's gate is `is not None` (spec 363); an absent file is `None`, so `discover_subscriptions` runs the code above. Current behavior, no seeder needed.
2. The two seed paths produce identical content. Boot seeder writes the pack's `subscribe:` (spec 269). CLI step 2 writes `discover_subscriptions(project_path)` (spec 326), which the run above shows IS the pack's `subscribe:` on any pack the boot seeder could fire on (the gate is "pack declares a non-empty list", spec 269).
3. The seeder saves no API calls. Spec 327 justifies step 2's cost: "That read can make live Slack and Linear API calls on such a team, once." `bobi/events/subscriptions.py:59-62` short-circuits before `Config.load`/`detect()` whenever `explicit` is truthy, which the stub above proves. A team with a pack list pays nothing. A team without one is never seeded at boot anyway.

The spec's own rejection of the alternative is false in this same PR:

```
spec 278-279: Seeding at boot rather than in `seed_workspace` (`bobi/install.py:174-192`)
              is deliberate. ... the eng-team pack has no `workspace/` directory
              to copy from today.
spec 421-424: **`agents/moda-eng-team/workspace/managed-repos.yaml`** (new)
              ... Seeded by the existing `seed_workspace` (`bobi/install.py:174-192`)
              ... so the mechanism is proven.
```

`seed_workspace` is key-agnostic - it `rglob`s and copies, it is not "taught a key" (`bobi/install.py:181-192`, read at `83bebe49`). After spec 421 lands, the premise at 279 no longer holds. So the spec rejects for `subscriptions.yaml` the exact mechanism it adopts two sections later for `managed-repos.yaml`, in the same pack.

**What survives:** the `file_lock` requirement from spec 274-276, relocated into CLI step 2 where the only write now happens (3 lines). Spec 199-200's summary loses "no change to `bobi/service.py`'s composition" and gains the unqualified "no change to `bobi/service.py`". Spec 208's file header says "Created by `subscriptions add`" instead of "Seeded from the agent pack on first manager boot". Migration (489-494) collapses to: no pack shape gets a workspace file until an operator or the director runs `add`, so no boot composes differently.

**One real cost, stated plainly:** before the first `add`, there is no file to `cat`. That costs one branch in `list` ("no workspace file; effective list is the pack's / auto-detected"), which spec 347 already specifies for the no-record case. If discoverability is judged worth more than the deleted code, the cheaper fix is a pack template under `moda-agents` `workspace/`, seeded by the already-shipping `seed_workspace` - still zero bobi-agent code - at the cost of the list appearing twice in the pack.

### K2. "Out of scope" is 5 bullets of restated argument, and Q1 is already answered by D4 [CUT]

**Spec lines:** 516, 517-519, 523-526, 527-528, 531-532, 536-543. 17 lines.

**Claim:** Five of eight out-of-scope bullets either echo a decision verbatim or argue against a design nobody proposed in revision 3. Q1 is decided by D4's own wording plus a fact the spec proves.

**Evidence:**

```
$ grep -n 'repo concept' <spec>
33:**D3.** ... (topics, not repos - no repo concepts in `bobi/`, and no `repos` CLI, per his Aug rulings).
516:- Any repo concept under `bobi/`, per D3 and Zach's August ruling.
```
516 restates D3. Round 1 filed this as C5 and round 2 re-filed it as CR2; still present.

```
$ grep -n 'filter_unauthorized' <spec>
333:5. For `add` only, call `authorize_resources(..., filter_unauthorized=False)` ...
385:Keep `filter_unauthorized=False` at `:1883`.
523:- Changing `_sync_saved_deployment`'s `filter_unauthorized=False` (`bobi/subagent.py:1883`).
532:  `authorize_resources` always attempts authorization, and `filter_unauthorized=False` leaves the server authoritative, which is enough here.
```
523-524 declares out of scope a change the spec never proposes; 385 already says "keep". 531-532 argues against a "pure grant-check that does not write", which no longer exists as an option once step 5 uses `False` - round 2 filed this as CR3, unadopted.
525-526 ("Making the pack layer presence-gated") points at spec 257-258, which already says "left alone, deliberately".
527-528 ("Branch-delete safety machinery ... per D5's accepted trade-off") restates spec 43 verbatim.

Q1: D4 says "Prefer reusing the PUT response if it already returns the accepted set; add a GET route only if needed" (spec 38). The spec then proves the response does return it, twice over:

```
$ sed -n '2296,2301p' event-server/test/core.spec.ts
		const result = await handleUpdateSubscriptions(store, "d1", "key1", {
			replace: ["inbox/test", "slack:T1:app:A1"],
		});
		expect(result.status).toBe(200);
		const body = result.body as { subscriptions: string[]; added: number; removed: number };
		expect(body.subscriptions).toEqual(["inbox/test", "slack:T1:app:A1"]);
$ sed -n '517,520p' tests/integration/test_event_server.py
        assert status == 200
        assert data["subscriptions"] == ["inbox/protocol-sync"]
        assert data["added"] == 0
        assert data["removed"] == 0
```

So option (b) is excluded by D4's condition being met, and option (c) is excluded by the spec's own rule at 348 ("a read command must not mutate"). Nothing is open.

**What survives:** three out-of-scope bullets - 520-522 (whatsapp/discord/new-workspace-slack do not apply live; nothing else states this), 529-530 (no generic workspace hot-reload; a reader coming from the superseded PR #956 overlay design needs this boundary), and one line folding 517-519 into an assertion. Q1 becomes one sentence in the `list` spec: `list` labels the record "last accepted at T", which is what the server accepted, not what it routes - routing additionally applies the delivery-time grant filter. Q2 stays: keep-vs-rollback on a rejected `add` is a real judgement with no decision covering it, and spec 342-344 specifies the exit code but not the persist.

### K3. The measured `_detect_github` block is severity evidence, not implementation input [SIMPLIFY]

**Spec lines:** 104-113. 10 lines -> 2. 8 saved.

**Claim:** The command block and the three-adapter walkthrough justify the severity of a hazard whose instruction is already one sentence at 117. It answered round 1's F3 and has served.

**Evidence:**

    $ sed -n '104,117p' <spec>
    104  Measured on this deployment at `83bebe49`:
    105
    106  ```
    107  $ PYTHONPATH=<main worktree> python3 -c "import bobi.events.adapters as a; from pathlib import Path; \
    108      print(a._detect_github(Path('/data/.bobi/agents/eng-team/run'), None))"
    109  ['github:moda-labs/bobi-agent']
    110  ```
    111
    112  So the fallthrough collapses 4 GitHub topics to 1.
    113  `bobi/events/adapters.py:139-142` names the Slack hazard in the same path (...), and `_detect_linear` (`:290-315`) subscribes to every Linear team the API key can see rather than MDS and MOD.
    114  The pack `subscribe:` is tolerable behind this gate because `<run>/package/` is read-only (`bobi/runtime_guard.py:143-153`).
    115  The new file is not: `<run>/workspace/` is writable, is edited by the CLI by design, and is advertised to the agent brain as its own scratch space (`bobi/prompts/resolver.py:86-97`).
    116
    117  A malformed workspace file must therefore fail loud, not fall through.

The mechanism (97-102) and the writable-vs-read-only asymmetry (114-115) are load-bearing: they say WHERE the read goes and WHY the two layers differ. Lines 104-113 between them are a measurement of blast radius. The implementer's instruction is spec 117, unchanged either way. (I did not re-run the `_detect_github` command; it reads git remotes and round 2 already executed it.)

**What survives:** "Measured at `83bebe49`, the fallthrough collapses 4 GitHub topics to 1 (`_detect_github` returns `['github:moda-labs/bobi-agent']`); `bobi/events/adapters.py:139-142` names the same hazard for Slack, and `_detect_linear` (`:290-315`) subscribes to every visible Linear team."

### K4. "Two stale facts in the current code" is a 7-line defense of two one-line fixes [CUT]

**Spec lines:** 180-187. 7 lines.

**Claim:** Round 2 filed this as CR1 and it was not adopted. The fixes belong in the PR; the section defending them does not.

**Evidence:**

```
$ grep -n 'client.py:50[12]\|client.py:518' <spec>
182:`bobi/events/client.py:501-502` claims the deaf-reconnect hook "re-registers on failure"; it does not.
185:`bobi/events/client.py:518-519` catches every non-protocol exception from the hook and logs it at `log.debug`.
389:Fix the stale "(and re-registers on failure)" clause at `:501-502`.
390:Raise the swallowed-exception log at `:518-519` from `debug` to `warning`, so a voided repair PUT is visible.
```
Both facts verified at the pin: `bobi/events/client.py:501-502` reads "re-asserts this deployment's subscriptions (and re-registers on failure)", and `:519` is `log.debug("Resubscribe after deaf reconnect failed: %s", e)`. Lines 389-390 carry the same two facts plus the action.

On "if something looks off, fix it along the way": the rule favors keeping the two CHANGES, and they stay. It does not favor a four-citation design section for them. The drive-bys do not muddy the change - the `debug` -> `warning` raise is the only trace of a voided repair PUT, which this spec's new all-or-nothing 400 path makes easier to hit.

**What survives:** spec 388-390 as-is. Nothing else.

### K5. The test plan is 20 items; 13 do distinct work [SIMPLIFY]

**Spec lines:** 443-481. 4 lines saved, 7 fewer tests to build.

**Claim:** Three merges and four drops, each against executed coverage.

**Evidence:**

Test 6 (spec 450, "a `remove` whose survivors include a `github:` topic granted at a prior boot returns 200 with no re-authorization") is already covered in substance, and subsumed by the merged 2+3 anyway:

```
$ sed -n '2284,2306p' event-server/test/core.spec.ts
	it("replaces stale subscriptions and removes old index entries", async () => {
		...
		store.seedGrant("slack", "T1", "bub_test");
		const result = await handleUpdateSubscriptions(store, "d1", "key1", {
			replace: ["inbox/test", "slack:T1:app:A1"],
		});
		expect(result.status).toBe(200);
```
The grant is written before the request and the replace passes - that is test 6's proposition. And spec step 5 has `remove` skip `authorize_resources` entirely, so test 3's `remove` leg exercises it by construction.

Test 8 (spec 458, "a worker session's deaf reconnect PUTs only `inbox/<worker>`") duplicates an existing test:

```
$ sed -n '110,136p' tests/test_event_subscription.py
def test_deaf_reconnect_uses_filtered_registered_subscriptions(...):
    """If fresh registration drops an unbacked global topic, the deaf reconnect
    resubscribe must not later PUT the raw unfiltered subscribe list."""
    ...
        _start_event_subscription("sess", ["github:o/r", "inbox/self"], project)
        on_deaf = mock_client.call_args.kwargs["on_deaf_reconnect"]
        on_deaf()
    assert mock_register.call_args.args[2] == ["inbox/self"]
    assert json.loads(put_reqs[0].content) == {"replace": ["inbox/self"], "protocol": EVENT_PROTOCOL}
```
It already asserts the deaf PUT is `["inbox/self"]`. Test 8 as written adds nothing; what IS uncovered is "the worker reads its OWN record, not the manager's", which needs a manager record present in the fixture. Rewrite the existing test, do not add one.

Test 10's first half (spec 460, "a PUT whose 200 body omits `subscriptions` leaves the record untouched") is exercised by nine existing tests by default:

```
$ grep -c 'json={"ok": True}' tests/test_event_subscription.py
9
```
Mishandling it breaks several of them. Keep only the second half (the next deaf reconnect replays non-empty rather than `{"replace": []}`), which is the G5 blocker and is not implied.

Test 14 (spec 471, "`workspace_subscriptions` returns `None` for an absent file and `[]` for `subscribe: []`") is the same two states tests 11 and 13 already require one layer up; fold it into test 13's parametrization, which extends an existing parametrized test:

```
$ sed -n '255,276p' tests/test_ingress.py
    ("subscribe: {}\n", []),                                      # unsupported shape
    ("", []),                                                     # absent
])
def test_ingress_and_discovery_read_subscribe_identically(...):
```

Tests 15 and 20, plus the second clause of 16 ("an `add` racing boot seeding is not lost"), die with K1. Spec 483 (the `tests/test_service.py:21-26` note) also dies: with no `bobi/service.py` change there is nothing to reassure anyone about.

**What survives:** 13 items. Merge 2+3 into one add-then-remove test asserting identity unchanged across both (spec 446 already couples them: "across both"). Merge 5 into 4 as one extra step (pause a monitor between boot and `add`). Drop 6, 14, 15, 20 and the second clause of 16. Rewrite 8 as an extension of `test_deaf_reconnect_uses_filtered_registered_subscriptions`. Keep 1, 2+3, 4+5, 7, 9, 10b, 11, 12, 13, 16a, 17, 18, 19. Test 1 stays genuinely new - the PUT path's `unauthorized_topics` is uncovered:

```
$ grep -n "unauthorized_topics" event-server/test/core.spec.ts
2030:		expect((badRes.body as { error: string }).error).toBe("unauthorized_topics");
```
One hit, in the register suite.

### K6. The same justification is stated up front and again at the instruction [SIMPLIFY]

**Spec lines:** 161-166, 173-175, 150-151. 9 lines -> 2. 7 saved.

**Claim:** "What the code actually does" states three facts that the design section then states again with the same citations. The instruction copy should survive, not both.

**Evidence:**

```
$ grep -n 'core.ts:1461\|core.ts:1498-1500\|core.ts:1509' <spec>
93:What a 201 proves is that the server stored the requested array verbatim (`core.ts:1461`) ...
166:That is what makes a `remove` safe without re-authorizing, since the server re-checks every surviving topic in the `replace` array (`core.ts:1509`).
171:`core.ts:1498-1500` returns `400 replace[] must not be empty`, so an empty replace is never a legal repair, which matters for the fallback rules below.
293:- `_register_with_retry` (`bobi/subagent.py:1834-1835`) records the `authorized` list ...; a 201 proves the server stored that array verbatim (`core.ts:1461`).
297:... a recorded `[]` would make every later repair PUT a `{"replace": []}` that the server rejects forever (`core.ts:1498-1500`) ...
335:   `remove` skips it: the server re-checks every surviving topic (`core.ts:1509`), but grants carry no TTL and have no revocation route ...
```
Each pair is justification-then-instruction with the same citation. 293, 297 and 335 are at the write sites and survive; 93, 166 and 171's trailing clause do not.

```
$ grep -n -i 'doctor' <spec>
142:### The ingress warning reads a different function, and `doctor` reads it too
150:That makes `bobi doctor` a reader too, through `check_ingress_reachability`: `bobi/doctor.py:51` runs the check and `:669-678` catches every exception into `CheckResult(ok=True, detail=f"skipped: {exc}")`.
151:A malformed workspace file would therefore fail the manager boot while `doctor`, the command an operator runs first, reported green.
261:The three other readers degrade safely and are unchanged, except for `doctor`.
264:`bobi/doctor.py:669-678` must change: a malformed workspace subscriptions file makes the ingress check FAIL with the path, instead of landing in the `ok=True, detail="skipped: ..."` arm that would hide the one thing the operator is looking for.
392:**`bobi/doctor.py`**
469:    The same test asserts `bobi/supervisor/snapshot.py:104-109` still returns an empty expectation list rather than raising, and that `bobi doctor`'s ingress check reports `ok=False` with the path.
```
Three prose statements of one two-line change, plus a test. 261-264 (the reader trace, where doctor belongs beside `snapshot.py` and `service.py`) and 393 (the action) survive, and 469 is a test asserting it, which is correct. 150-151 is the redundant one.

Spec 173-175 ("So D4's 'topics the server silently dropped' is two different things ... Neither needs a GET route") answers D4's wording; the answer belongs in K2's single asserted sentence, not here.

**What survives:** at 166, "Grants carry no TTL (`event-server/worker/src/index.ts:147-160`, no `expirationTtl`) and there is no revocation route." - verified, `putResourceGrant` passes no TTL. The `filter_unauthorized=False` policy keeps its full statement once, at 161 (it is cited from two code comments and is the F6 blocker's resolution); the rotation hypothetical at 163 goes.

### K7. Release-ordering pin mechanics are deploy trivia the spec itself calls stale [SIMPLIFY]

**Spec lines:** 500-502, 511. 5 lines saved.

**Claim:** Five workflow citations establish a version number the next sentence says to re-read, and 511 argues the opposite of 512.

**Evidence:**

```
$ sed -n '500,502p' <spec>
`moda-labs/moda-agents` pins the fleet's bobi version in `.github/fleet-version`, read by `deploy-agent-teams.yml:91` and `:177` and by `team-images.yml:139`, and rewritten by `version-gate.yml:100` after `ci-canary` proves a version.
(`release-fleet.yml:342` installs `inputs.version`, the candidate under test, not the pin.)
At `bf9d1088` the pin reads `BOBI_VERSION=0.59.0`; re-read it at approval time, since `version-gate.yml` moves it automatically.
$ sed -n '511,512p' <spec>
The new `workspace/managed-repos.yaml` seed relies on `seed_workspace`, which already ships, so D5's pack half has no hard dependency on the new CLI.
But the director prompt would name commands that do not exist yet on a fleet pinned to the pre-change version, so ordering it after the pin bump keeps the prompt honest.
```
Round 1's F4 was that a stated pin value goes stale; revision 3 fixed it by saying so, which makes the value itself worthless to state. 511 and 512 are a concession and its rebuttal; only the rebuttal drives the ordering.

**What survives:** "The fleet pin in `.github/fleet-version` is moved automatically by `version-gate.yml:100`, so step 2 must follow step 1: the director prompt would otherwise name commands a pre-change fleet does not have." Plus the three-step list at 504-510, unchanged.

### K8. Four single-line corroborations [CUT]

**Spec lines:** 54, 86, 89, 253. 4 lines.

**Claim:** Each repeats the sentence above it or adds a cost nobody acts on.

**Evidence:**

```
53:`bobi/session.py:1657-1660` then prepends `inbox/<session name>` before registering ...
54:`docs/EVENT_SERVER.md:396-398` states the same composition, including "the session's own `inbox/<self>`".
```
54 corroborates 53 from a doc. 53 is the code.

```
79:`bobi/events/state.py:32-64` keeps two kinds of per-session state under `<run>/state/` ...
86:A third per-session file follows the pattern the other two already set.
88:`event-server/core/src/core.ts:1537-1540` returns `{subscriptions, added, removed, protocol}` ...
89:The data D4 asks for is already on the wire, one line from where it would be written.
```
86 restates 79's conclusion; 89 is rhetoric on 88.

```
253:The file is therefore read twice on a boot where it is absent, once by each function, which costs one extra `Path.exists()`.
254:One window follows from that: a file CREATED between the two reads is read from inside the `try`, so if it is also malformed the exception is swallowed for that one boot.
```
254-255 documents a real trap and stays. 253's cost accounting is one syscall; nobody changes a decision on it.

**What survives:** nothing from these four lines; 254-255 keeps its own opening ("The file is read twice on an absent-file boot, once by each function. One window follows: ...").

## Design elements: keep or cut

**The per-session accepted-set file (`<run>/state/subscriptions/<session>.json`) and its three functions - KEEP.**
Round 1's cut was overruled because the deaf hook reads it. That reason is correct but weak; here is the stronger one, which defeats every smaller alternative I could construct.
`active_subscriptions` is a closure local in `_start_event_subscription` (`bobi/subagent.py:1765`, reassigned at `:1835` and `:1892`, read at `:1982`), so another process cannot update it. There is no reload path either:
```
$ grep -rn "SIGHUP\|signal.signal\|def reload\|_reload" bobi/ --include=*.py
bobi/launch_admission.py:116:            binding_signal=signal,
bobi/service.py:678:    signal.signal(signal.SIGTERM, _handle_term)
bobi/supervisor/supervision.py:820:                signal.signal(sig, _on_term)
bobi/kb/sidecar.py:151:    signal.signal(signal.SIGTERM, _shutdown)
bobi/otel/config.py:250:        signal=signal,
```
Three SIGTERM shutdown handlers and two unrelated `signal=` kwargs. No SIGHUP, no reload. A file is the only channel.
The obvious cheaper file is the workspace file itself, with the hook re-reading it. That breaks on two counts.
First the leak: a worker's `subscribe` is `["inbox/<self>"]` (`bobi/session.py:1657-1660`, `:1673`, `:1710`), so a hook that reads the workspace file unconditionally PUTs the manager's `github:`/`slack:`/`linear:` topics onto the worker's own deployment - round 1's F2 verbatim. You would need a per-session gate; `has_external` (`bobi/subagent.py:1760`) exists and would serve.
Second, and fatal, the register path drops topics and the workspace file does not know which:
```
$ sed -n '680,682p' bobi/events/server.py
def authorize_resources(base_url: str, cfg, subscribe: list[str],
                        bubble_id: str, bubble_key: str,
                        *, filter_unauthorized: bool = True,
```
`_register_with_retry` takes that default (`:1805-1809` -> `:1820`), and `active_subscriptions = list(authorized)` at `:1835`. The existing test at `tests/test_event_subscription.py:110-136` pins exactly this: pass `["github:o/r", "inbox/self"]`, and `mock_register.call_args.args[2] == ["inbox/self"]`. A hook rebuilt from the workspace file would re-add the dropped topic, and the server 400s the whole repair PUT (`core.ts:1508-1512`) with the exception swallowed on a daemon thread. The recorded set cannot have that bug, because it records what was accepted.
Updating `active_subscriptions` in process is not an option for the same cross-process reason. The file survives the comparison.

**`seed_workspace_subscriptions` at boot - CUT.** See K1. It does not earn the `bobi/service.py` change, the pack-non-empty gate, or the 38 lines of design and justification that exist only to make the gate correct.

**Moving `_put_subscriptions` into `bobi/events/server.py` - KEEP.**
The CLI cannot call `bobi/http.put` and be done: `bobi/http.py:78-82` is a five-line pooled wrapper, and the closure around it (`bobi/subagent.py:1926-1946`) is 20 lines of protocol handling the CLI needs - `protocol_payload()`, `raise_for_protocol_error`, `raise_for_status`, the `"error" in data` guard, `validate_server_response` (`bobi/events/protocol.py:55-70`), and step 9's `UnauthorizedTopics`. Calling `http.put` directly means a second copy of all of it in `cli.py`.
The closure genuinely cannot be reused in place: it closes over `es_url` (`:1755`) and `protocol_payload`. No cycle - `bobi/events/server.py` imports only `launch_stamp`, `events.artifact` and `fsutil` at module level, and `bobi/cli.py` already imports from it at `:3176`, `:3218`, `:3269`.
The one cost the extraction adds is real and the spec is right to carry it: `UnauthorizedTopics` is a plain `Exception` (`bobi/events/server.py:627-634`), not an `httpx.HTTPStatusError`, so without the new classifier branch at spec 384 the boot message degrades from "resource grants rejected (HTTP 400)" (`bobi/subagent.py:1909-1910`) to "unexpected subscription failure" (`:1916`). Three lines, needed.

**The `doctor` change - KEEP the change, CUT one of its three statements.**
The change is required, not cosmetic: `bobi/doctor.py:676-678` is `except Exception: return CheckResult(..., ok=True, detail=f"skipped: {exc}")`, and `bobi/ingress.py:83` calls `explicit_subscriptions`, which the spec makes the workspace reader. A malformed workspace file would fail the manager boot while `doctor` reported green. Two lines in the change list (393) plus one line in the reader trace (264) is the right size; spec 150-151 is the third telling. See K6.

**The `bobi/events/client.py` drive-bys - KEEP the fixes, CUT the section.** See K4. Both facts are true at the pin, both fixes are one line, and the `warning` raise is the only visibility for a repair PUT this spec makes easier to void. They do not muddy the change; the 7-line section defending them does.

**The 20-item test plan - 13 items.** See K5.

**The `managed_repos` / D5 half - KEEP in this spec.**
It is a separate PR by necessity (different repo; spec 410 says so), but it is not separable work, because the pack change breaks a CI gate in the other repo:
```
$ grep -rn "managed_repos" bobi/ ; echo "bobi/ exit=$?"
bobi/ exit=1
$ sed -n '85,94p' scripts/check-deploy-compose.py          # moda-agents @ bf9d1088
                managed_repos = {
                    str(binding.get("repo")): str(binding.get("tracker"))
                    for binding in (cfg_data.get("managed_repos") or [])
                    if isinstance(binding, dict)
                }
                if managed_repos.get("moda-labs/moda-skills") != "github-issues":
                    failures.append(
                        f"{name}: composed package must manage moda-skills "
                        "with the github-issues tracker"
                    )
```
Remove the key without touching that script and the gate fails: `cfg_data.get("managed_repos") or []` yields `{}`, so the moda-skills lookup returns `None` and a failure is appended. A reader of this spec needs that coupling and the three-step ordering; splitting it into a second spec is how it gets missed. 23 lines for a cross-repo CI coupling is proportionate. Only the pin mechanics inside Release ordering are padding (K7).

## Facts stated more than once

| Fact | Spec lines | Survives |
|---|---|---|
| `inbox/<session>` is prepended per session and must not be lost | 53, 54, 56, 68, 228, 300, 332, 447 | 53, 56, 68 (the trap), 332 (the construction), 447 (the test). Cut 54 (doc corroboration of 53). 228 and 300 are distinct statements about the file and the hook. |
| `{"replace":[...]}` is authoritative | 34, 56 | 56 (with the `core.ts` cite). 34 is D1-D5 verbatim and is not cuttable. |
| a 201 proves the server stored the array verbatim (`core.ts:1461`) | 93, 293 | 293 (at the write site) |
| empty `replace` -> 400 (`core.ts:1498-1500`) | 171, 297 | 297 (where the no-empty-record rule is stated) |
| the server re-checks survivors (`core.ts:1509`) | 166, 335 | 335 (CLI step 5) |
| grants have no TTL and no revocation route | 163, 165, 335, 450 | 165 (one line, with the `index.ts` cite) + 335. Cut 163's rotation hypothetical; test 6 goes with K5. |
| `filter_unauthorized=False` is the deliberate existing-deployment policy | 161, 163, 173, 333, 385, 523, 532 | 161 (full statement, two code comments) + 333 (the instruction) + 385 (keep it). Cut 163, 173, 523-524, 532. |
| a malformed workspace file must fail loud, not fall through | 102, 117, 244, 250 | 117 (the rule) + 250 (the placement that implements it). 102 is about the PACK path and 244 is the reader contract; both distinct. |
| a malformed file makes `doctor` read green | 150-151, 264, 393 | 264 + 393 |
| presence, not truthiness | 119 (heading), 234 (heading), 257, 272, 525 | 234's section + 257. 119 goes with K1; 525 goes with K2. |
| "no repo concepts in `bobi/`" | 33, 516 | 33 (D3 verbatim) |
| branch-delete authority is an accepted trade-off | 43, 527 | 43 (D5 verbatim) |
| no GET route is needed | 175, 517-519, 540 | one asserted line replacing Q1 |

## Total

549 lines before, about 449 after. Roughly 100 lines, of which 46 come from one design cut and the rest from prose that states a fact twice or defends a decision already made.

What the reader loses: the measured blast radius of the auto-detect fallthrough (K3), the fleet-wide pack census (K1), a doc corroboration of a code fact (K8), and five out-of-scope bullets that restate a decision (K2).
What the reader keeps: every trap the two prior rounds found - the `inbox/` loss, the cross-session leak, the credential-record clobber, the register-path drop, the malformed-file fall-through, the `doctor` green-while-broken arm, the monitor re-sync, the all-or-nothing 400, and the empty-record permanent-void - each stated once, at the place an implementer acts on it.
What the implementer loses: one change site in `bobi/service.py`, one new function, one gate, and seven tests.
