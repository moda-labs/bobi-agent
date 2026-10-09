# Round 4 review: revision 3 -> revision 4 folds

## Verdict

Eleven of the thirteen folds are sound, the seeder cut is correct in its central claim (I reproduced the fall-through by execution and found no dangling reference to `seed_workspace_subscriptions` anywhere in the spec), and every citation the edits added or corrected checks out at the pin.
The spec is not yet implementable as written, for one reason: the H4 fold states a guarantee its own lock placement does not deliver.
The CLI holds `file_lock` on the accepted-set record across steps 5-8, but the two manager write sites take that lock only around the record WRITE, which sits after their PUT (`bobi/subagent.py:1891` PUTs, `:1892` assigns), so a boot that applies during the CLI's locked window is invisible to step 5's abort check and the CLI's PUT still clobbers it.
In the interleaving the lock actually forces, the end state is the record disagreeing with the server in the direction that reverts the operator's `add` on the next deaf reconnect, which is the exact failure this whole design exists to prevent.
Second, the seeder cut left one hole it did not fill: `list`'s no-file branch is named as a cost but never specified, and that branch is every team's state for the entire window before the first `add`.
Both are small spec edits.

## Fold audit

- K1 (boot seeder deleted) -> PARTIAL: the fall-through claim reproduces exactly and nothing dangles, but `list`'s new no-file branch is unspecified (J3) and one surviving sentence went stale (J9).
- H1 (test-plan blocker) -> FIXED: the unsatisfiable assertion is gone, `:456`/`:499` are exact, and test 7 is buildable and red pre-change.
- H2 (`list` flags any difference) -> FIXED: one direction only, and I re-derived the 17 derived topics independently (1 inbox + 12 monitor + 4 lifecycle).
- H3 (network inside the lock) -> FIXED: identity resolves first, `discover_subscriptions` moved outside `file_lock`, and the false "any network or process work" claim is replaced with the true one.
- H4 (no lock across read-PUT-write) -> BROKEN: see J1 and J2.
- H5 (`doctor` cannot discriminate) -> FIXED: both existing arms described exactly, the new arm first is valid Python with no shadowing (proven by execution), and the exception does reach that arm because `bobi/ingress.py:83` does not wrap the call.
- H6 (no pack-only reader) -> FIXED: moot with the seeder gone, and the spec says so.
- H7 (raw vs interpolated seed) -> PARTIAL: revision 4 picks RESOLVED, but only inside test 13; the Design section never states it, nor the consequence that env coupling is dropped at the first `add`.
- H8 (vacuous tests 10/20) -> FIXED: test 10 became test 7 with two PUTs, test 20 is dropped.
- H9 (authorize the whole list) -> FIXED, and it loses nothing durable: `_sync_saved_deployment` re-authorizes the whole composed list with `filter_unauthorized=False` at every boot (`bobi/subagent.py:1880-1886`), so a record topic with no grant gets one at the next start.
- H10 (`:838-842`) -> FIXED: `:840-842` is exact.
- H11 (docstring attribution) -> FIXED: the sentence is gone.
- H12 (`_safe_session`) -> FIXED: `bobi/events/state.py:28-29` is exact.
- Old Q1 closed -> JUSTIFIED: both proofs verified; one residual noted below.
- K2-K8 (cruft) -> FIXED, one exception (J8).

## Findings

### J1. The accepted-set lock does not close the race it says it closes, and the step-5 abort cannot see the only interleaving that matters [BLOCKER]

**Claim:** The CLI holds the record lock across steps 5-8, but the manager's two write sites lock only the record write, which happens AFTER their apply, so a boot that applies during the CLI's window is serialized behind the CLI's lock and is therefore invisible to step 5's "abort if it changed since step 2" check.

**Evidence:** the manager's apply and its record write are different lines, and the write is second.

```
$ awk 'NR>=1890 && NR<=1893 {printf "%d: %s\n", NR, $0}' main-verify-1116/bobi/subagent.py
1890:         try:
1891:             _put_subscriptions(dep, key, authorized)
1892:             active_subscriptions = list(authorized)
1893:             return dep, key

$ awk 'NR>=1822 && NR<=1835 {printf "%d: %s\n", NR, $0}' main-verify-1116/bobi/subagent.py   (register path)
1822:                     dep, key = register(
1823:                         url, session_name, authorized,
...
1834:                 save_deployment_state(project_path, session_name, dep, key)
1835:                 active_subscriptions = list(authorized)
```

The spec scopes the CLI's lock explicitly and the manager's not at all:

```
spec 275: ... all three writers take `file_lock` on this file, and the CLI holds it across read, PUT and record.
spec 357: Every write takes `file_lock` on the record.
spec 263: Two sites write it, both on the line where they already assign `active_subscriptions`.
spec 310: 5. Under `file_lock` on the accepted-set record, held from here through step 8: re-read the record,
             and abort with "the manager restarted, re-run the command" if it changed since step 2.
```

`file_lock` is blocking with no timeout, so the manager's write waits rather than failing:

```
$ awk 'NR>=179 && NR<=184 {printf "%d: %s\n", NR, $0}' main-verify-1116/bobi/fsutil.py
179:     with open(lock_path, "a+") as lock_file:
180:         fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
```

**Why it matters:** record holds R0. The operator runs `monitors pause <m>` (`bobi/cli.py:2992`, writes `package/monitors.yaml`, no re-PUT). The operator runs `subscriptions add github:b`.
Step 2 reads R0. Step 5 takes the lock, re-reads R0, sees no change, builds `R0 + github:b`.
The manager restarts: `_sync_saved_deployment` PUTs the recomposed R1 at `:1891` (server set = R1), then blocks at `:1892` on the CLI's lock.
Step 8 PUTs `R0 + github:b`; `core.ts:1522-1530` removes every topic not in it, so R1's monitor change is dropped from the index.
The CLI records `R0 + github:b` and releases; the manager's blocked write then lands and overwrites the record with R1.
End state: server = `R0 + github:b`, record = R1. The next deaf reconnect replays R1 and silently reverts the operator's `add`.
Step 5's abort can only fire if the manager's record write lands between step 2's read and step 5's lock, which on a team that already has the workspace file is two file reads apart.

**Suggested fold:** give the manager sites the same scope as the CLI's. In the Design section replace "Every write takes `file_lock` on the record" with: "Both manager sites hold `file_lock` on the record across their apply AND their write - `_sync_saved_deployment` from the `authorize_resources` call (`:1880`) through `:1892`, and `_register_with_retry` from `register()` (`:1822`) through `:1835` - so the CLI's steps 5-8 and a boot's apply are mutually exclusive and step 5's abort can observe a restart that has already applied."

### J2. The step-5 abort is a third terminal outcome with no exit code and no statement about the already-written workspace change [MAJOR]

**Claim:** Revision 4 moves the persist to step 4 and adds an abort at step 5, so the abort now fires with the workspace file already mutated, and the spec's own outcome table does not list it.

**Evidence:**

```
$ sed -n '325,327p' plans/2026-10-09-workspace-event-subscriptions.md
Two states mean "nothing live to update" at step 2: no deployment record (the manager is not running), and a deployment record with no accepted-set file (the running manager predates this version, so every `add` is persist-only until it restarts once).
Both persist and exit zero.
A `put_subscriptions` rejection at step 8 persists and exits non-zero.
```

Step 5's abort is neither of those two, and it is not the step-8 rejection.
The spec specifies the persist/exit outcome for every other terminal path, including step 9's rejection ("The workspace change written at step 4 is kept; see Q2", "exit non-zero") and step 6's bubble failure ("the command FAILS with that fact").

**Why it matters:** an implementer has to invent the exit code and the persist semantics for the one path that is a concurrency abort. A script driving `add` cannot tell "aborted, nothing live, file already changed" from "applied". The recovery is in fact sound - the re-run's step 4 is idempotent and step 5 then applies to the fresh record - but nothing says so, so the operator-facing message is also unspecified beyond "re-run the command".

**Suggested fold:** one line after spec 327: "A step-5 abort also persists and exits non-zero; the re-run is idempotent, because step 4's edit is already in the file and step 5 re-applies it to the fresh record."

### J3. The seeder cut left `list`'s no-file branch unspecified, and that branch is every team's state until the first `add` [MAJOR]

**Claim:** With the file created lazily, `list` has no persisted list to compare for the whole pre-first-`add` window, and the spec names the branch as a cost without saying what it does.

**Evidence:**

```
$ sed -n '247p;329,333p' plans/2026-10-09-workspace-event-subscriptions.md
The cost is that `cat workspace/subscriptions.yaml` shows nothing until the first edit, so `list` carries a branch for it.
`list` prints the persisted workspace list and the recorded accepted set with its timestamp.
It flags ONE direction: topics in the persisted list that are absent from the accepted set, which is what D4's "topics the server silently dropped" means.
The other direction is expected and is never flagged, because `inbox/<session>`, the monitor keys and the lifecycle keys are in the accepted set by design and never in the file: on this team that is 17 topics, so flagging "any difference" would fire on every run of a healthy team and bury the one real signal.
Those 17 print as a separate derived group, labelled as such.
With no workspace file it says the effective list is still the pack's or auto-detected, and with no record it says no live set is recorded, naming which reason applies.
```

Line 333 specifies a MESSAGE, not a comparison, so the flag direction at line 330 has no input in the no-file case.
Resolving the effective list instead would make a read command do network work on an auto-detecting team:

```
$ awk 'NR>=297 && NR<=306 {printf "%d: %s\n", NR, $0}' main-verify-1116/bobi/events/adapters.py
297:     try:
298:         resp = pooled.post(
299:             "https://api.linear.app/graphql",
300:             json={"query": "{ teams { nodes { key } } }"},
...
305:             timeout=5.0,
```

The spec carries that warning for `add`/`remove` at step 3 ("This runs OUTSIDE `file_lock`, because on an auto-detecting team it makes live Slack and Linear API calls ... The command says in its own output that it made those calls") and nowhere for `list`.

**Why it matters:** under revision 3 any pack with a `subscribe:` list had the file from its first boot, so `list` always had a persisted list. Now D4's read-back signal is unavailable on every deployment until someone runs `add`, which is the window where an operator is most likely to be checking. And if the implementer closes the gap the obvious way, `bobi agent <name> subscriptions list` becomes a read command that POSTs the real Linear key, undoing H3's fix on the read path.

**Suggested fold:** replace line 333's first clause with: "With no workspace file, `list` resolves the effective list with `discover_subscriptions`, labels it as the pack's or auto-detected rather than persisted, says in its own output when that resolution made live Slack or Linear calls, and still flags the one direction against it."

### J4. Test 7 claims both write sites but mechanizes only one, and "a LATER PUT" has a vacuous reading [MINOR]

**Claim:** The replacement for the H1 blocker names a scope its described mechanics do not reach.

**Evidence:**

```
$ sed -n '447,448p;391p' plans/2026-10-09-workspace-event-subscriptions.md
7. Both write sites record, and an omitted echo does not erase: a PUT returning `{"subscriptions": [...]}` is recorded, a LATER PUT whose 200 body omits `subscriptions` leaves that record intact, and the next deaf reconnect replays it rather than `{"replace": []}`.
   Two PUTs are required; a single `{"ok": True}` PUT proves nothing, which is why the existing tests at `:456` and `:499` are left alone.
The write sites are covered by new test 7 instead.
```

Two PUTs only exercise `_sync_saved_deployment` (`:1891-1892`). The register site (`:1834-1835`) records "the `authorized` list it passed to `register()`", which needs a leg with no saved deployment, and no test in the plan drives it.
Separately, if the "LATER PUT" is the deaf hook's, the clause is vacuous: the spec lists the hook as a reader only, so nothing writes the record on that path and "leaves that record intact" is true by construction.

**Suggested fold:** split the clause. "a fresh register records the `authorized` list it passed (`:1834-1835`); a later session start whose PUT returns `{"subscriptions": [...]}` records the echo (`:1891-1892`); a third start whose 200 body omits `subscriptions` leaves that record intact."

### J5. Q1's option set omits the one option the same spec already uses, so the recommendation is being compared against the expensive alternative only [MINOR]

**Claim:** Q1 offers lazy creation versus a 40-line boot seeder plus a gate plus three tests, and never mentions a pack `workspace/` template seeded by the already-shipping `seed_workspace`, which this spec adopts for `managed-repos.yaml` 100 lines earlier.

**Evidence:**

```
$ sed -n '412p;516p' plans/2026-10-09-workspace-event-subscriptions.md
Seeded by the existing `seed_workspace` (`bobi/install.py:174-192`), which copies pack `workspace/` templates only if absent, so a later install never overwrites director edits.
If you want the file present from the first boot, say so and the seeder plus its gate come back, at about 40 lines and three more tests.

$ awk 'NR>=181 && NR<=192 {printf "%d: %s\n", NR, $0}' main-verify-1116/bobi/install.py
181:     src = pack_dir / "workspace"
182:     if not src.is_dir():
183:         return
184:     dest = paths.workspace_dir(project_path)
185:     for f in sorted(src.rglob("*")):
...
190:         elif not target.exists():
191:             target.parent.mkdir(parents=True, exist_ok=True)
192:             shutil.copy2(f, target)
```

`seed_workspace` is key-agnostic and copy-if-absent, and the companion PR creates `agents/moda-eng-team/workspace/` for the first time anyway.
That option costs zero bobi-agent code and needs no absent-vs-empty gate, because the pack author writes the file explicitly.
Its real cost is the eng-team list appearing twice in the pack (`subscribe:` and the template), with only `subscribe:` checked by `scripts/check-deploy-compose.py:76-83`.

**Why it matters:** Q1 is a decision request. As framed, the alternative to "keep it lazy" is the most expensive version of presence, so the comparison is loaded. It also matters for D2: a pack template satisfies D2's second clause ("the pack `subscribe:` is only the SEED") from install, which lazy creation does not do for the pre-`add` window.

**Suggested fold:** add one sentence to Q1 naming the third option and its duplication cost.

### J6. The header's line-count claim is off by two [NIT]

**Claim:** The spec says 29 lines shorter; it is 27.

**Evidence:**
```
$ git show 5b3e8740:plans/2026-10-09-workspace-event-subscriptions.md | wc -l
549
$ git show 464496a4:plans/2026-10-09-workspace-event-subscriptions.md | wc -l
522
$ sed -n '14p' plans/2026-10-09-workspace-event-subscriptions.md
- `plans/reviews/2026-10-09-952-subscriptions-review-3.md` (1 blocker; the design lost its boot-time seeder, and the spec got 29 lines shorter while folding that blocker and four majors).
```

**Suggested fold:** 27, or drop the number.

### J7. "answer every request with a bare `{"ok": True}`" is false for one of the two tests it describes [NIT]

**Claim:** The `:456` test answers the authorize POST with a 403, not `{"ok": True}`. Round 3's H1 said "answer the PUT", which was precise; the fold widened it.

**Evidence:**
```
$ awk 'NR>=452 && NR<=456 {printf "%d: %s\n", NR, $0}' main-verify-1116/tests/test_event_subscription.py
452:     def handler(request: httpx.Request) -> httpx.Response:
453:         captured.append(request)
454:         if request.method == "POST" and str(request.url).endswith("/resources/authorize"):
455:             return httpx.Response(403, json={"error": "forbidden"})
456:         return httpx.Response(200, json={"ok": True})
```
The conclusion is unaffected: the PUT does get `{"ok": True}`, so `put_subscriptions` returns `None` and nothing is recorded.

**Suggested fold:** "answer the PUT with a bare `{"ok": True}`".

### J8. The cruft cut removed the only statement of what the corrected `client.py:501-502` docstring should say [NIT]

**Claim:** The change list still orders the fix, but the fact that makes the clause stale is now nowhere in the spec.

**Evidence:**
```
$ sed -n '376p' plans/2026-10-09-workspace-event-subscriptions.md
Fix the stale "(and re-registers on failure)" clause at `:501-502`.
$ grep -c '1838\|retry pending' plans/2026-10-09-workspace-event-subscriptions.md
0
```
The deleted rev-3 text carried it: "Only `_register_with_retry` unlinks the cursor and re-registers (`bobi/subagent.py:1838`); `_sync_saved_deployment`'s PUT error path (`:1894-1924`) explicitly retains both and says so at `:1917-1920`."
Both halves still hold at the pin (`:1838` is `cursor_path.unlink(missing_ok=True)` inside `_register_with_retry`, and `client.py:501-502` still reads "(and re-registers on failure)").

**Suggested fold:** append to line 376: "it is only `_register_with_retry` that re-registers (`:1838`); the PUT error path retains both (`:1894-1924`)."

### J9. "the boot PUT is built from the workspace file" is now only conditionally true [NIT]

**Claim:** An unchanged sentence became stale by the seeder cut: with no file, the boot PUT is built from the pack list or auto-detection.

**Evidence:**
```
$ sed -n '337p' plans/2026-10-09-workspace-event-subscriptions.md
An `add` issued while the manager is booting can lose the race: the boot PUT is built from the workspace file, so if the CLI's write lands first the topic is live, and if it lands second the file is ahead of the server until the next start.
```

**Suggested fold:** "the boot PUT is built from whatever `discover_subscriptions` resolves, which is the workspace file once it exists".

## Cruft pass: anything load-bearing lost

I checked each removal against the place an implementer acts on the trap it documented.

- Fleet pack census (K1, 13 lines). The trap survives at line 241 ("a pack-non-empty gate (the pack reader cannot tell an absent `subscribe:` from `subscribe: []`, which is what made the first attempt at this write an authoritative empty list for most of the fleet)") and at line 512 ("the auto-detecting majority of the fleet"). With no seeder, no implementer acts on it. The quantification (5 of 7 packs) is gone, which only weakens Q1's evidence if the user chooses to restore the seeder. Acceptable.
- "Two stale facts in the current code" (K4, 7 lines). The `:518-519` half fully survives: the change list at line 377 orders the `debug` -> `warning` raise and test 8 (line 449) asserts it fires. The `:501-502` half lost its content; that is J8.
- Five "Out of scope" bullets (K2). All five survive at the instruction: "no repo concepts" at D3 (line 37), `filter_unauthorized=False` at line 372 ("Keep `filter_unauthorized=False` at `:1883`"), presence-vs-truthiness at line 217 ("The pack's own truthiness gate at `:61` is left alone, deliberately") with its test pin at 218, branch-delete at D5 (line 47), and the pure-grant-check option is excluded by step 6's own text at lines 313-315.
- Four single-line corroborations (K8). Nothing lost. `docs/EVENT_SERVER.md:396-398` is still in the docs change list at line 394, so the implementer still knows to touch it.
- Measured `_detect_github` block (K3). The fact survives with its value at line 105, and it still reproduces: `_detect_github(Path('/data/.bobi/agents/eng-team/run'), None)` returns `['github:moda-labs/bobi-agent']` against 4 `github:` topics in the pack. Only the command is gone.
- Release-ordering pin mechanics (K7). The ordering rule survives at line 492 and the pin mechanism at line 484 (`version-gate.yml:100` verified: `sed -i -E "s#^BOBI_VERSION=${old}\$#BOBI_VERSION=${NEW}#"`). The deleted reader citations (`deploy-agent-teams.yml:91`/`:177`, `team-images.yml:139`) drive no action in either PR. The deleted concession about `seed_workspace` already shipping survives at lines 412-413.

Nothing load-bearing was lost except J8.

## On the seeder cut specifically

- The fall-through claim is VERIFIED by execution, against `main-verify-1116` (clean worktree, `83bebe49`), with both `detect` and `Config.load` patched to raise:
```
module file: /data/.bobi/agents/eng-team/run/worktrees/main-verify-1116/bobi/events/subscriptions.py
project: /tmp/rev4probe.cGDo workspace file exists: False
explicit_subscriptions : ['github:moda-labs/lightweave', 'linear:MDS']
discover_subscriptions : ['github:moda-labs/lightweave', 'linear:MDS']
auto-detect calls made : []
```
This matches the spec's block at lines 233-239 exactly.
- No dangling reference: `grep -n 'seed_workspace_subscriptions'` over the spec exits 1. The change list, the test plan and Migration are all clean, and "no boot-time seeder" is stated consistently at lines 17, 158, 228-231 and 350, with `bobi/service.py` unchanged at 158, 352 and 470.
- What breaks: only `list`'s no-file branch (J3) and the stale sentence at line 337 (J9). Every other workspace-file mention still reads correctly under lazy creation. The double-read window paragraph (lines 213-215) is in fact MORE coherent now, because the only creator during a boot is a concurrent CLI run rather than the boot itself.
- D2: the reading holds for clause 1 and is false for clause 2 during the pre-`add` window. Before the first `add` the pack `subscribe:` is not a seed, it is the live source of truth, read on every boot and PUT to the server on every start. The spec is honest about this - Q1 quotes D2, names it as a reading, names the cost, and offers to restore the seeder with a price - but it understates it by calling it discoverability when the sharper statement is that clause 2 is simply not yet true. It also loads the comparison by omitting the pack-template option (J5). My own read: lazy is the better engineering, and the pack template is the way to satisfy D2's letter at zero framework cost, so Q1 should put all three in front of the user.
- The `file_lock` story on the workspace file is coherent. I verified nothing else in `bobi/` writes a `subscriptions.yaml` under `<run>/workspace/`: the only copy paths are `seed_workspace` (`bobi/install.py:181-192`, gated on a pack `workspace/` template existing, which no pack ships for this key) and `bobi/compose.py:360-366` (writes the pack image, not the run workspace). Two qualifiers worth one line in the spec: the companion PR creates `agents/moda-eng-team/workspace/` for the first time, so a future `subscriptions.yaml` template there would be an UNLOCKED second writer; and `bobi/prompts/resolver.py:86-97` points the agent brain at that directory ("holds domain files and work products"), so a hand edit bypasses the advisory lock by construction (`bobi/fsutil.py:169`: "Advisory, so it only serializes writers that also take it").

## Consistency check

- Test numbers: 15 items, numbered 1-15, no gaps. Both prose references resolve: "new test 7" (line 391) and "Test 14" (line 478).
- `See Qn`: "See Q1" (line 248) -> Q1 at 510. "see Q2" (line 323) -> Q2 at 518. No dangling Q.
- Change list vs Design: matched file by file, nothing in one and missing from the other. `bobi/cli.py`'s three edits match the `monitors` precedent (`main.add_command(monitors)` at `:3155`, the group list at `:4168`, the pop list at `:4176-4181`).
- Header: "after three independent review rounds" is accurate, all three review files are present. The line-count claim is wrong by two (J6).
- Em dashes: zero on added lines, and zero in the whole file.
- One sentence per line: holds on all 140 added lines (checked mechanically for a sentence-ender followed by a capital or a backtick).
- Citations added or corrected by this revision, all verified at `83bebe49` / `bf9d1088`: `bobi/events/server.py:840-842` (exact, H10 fixed), `:682` (`filter_unauthorized: bool = True`), `:781-784` (the per-topic `_authorize_one_resource` call), `bobi/events/state.py:28-29` (`_safe_session`), `:44-52`, `:79`, `bobi/subagent.py:836-838` ("caller-chosen, always"), `:1948-1955`, `bobi/doctor.py:51`, `:673-675` (`except FileNotFoundError` -> "no agent config"), `:676-678` (`except Exception` -> "skipped:"), `bobi/events/adapters.py:290-315` (`_detect_linear`, 5s POST), `bobi/fsutil.py:100`, `:162`, `bobi/service.py:135-144`, `bobi/cli.py:2965`/`:2992`/`:3016`, `bobi/install.py:174-192`, `bobi/prompts/resolver.py:86-97`, `bobi/ingress.py:83`, `core.spec.ts:2300-2301`, `tests/integration/test_event_server.py:517-520`, `tests/test_event_subscription.py:456`/`:499`/`:469-471`/`:514-515`, `agents/moda-eng-team/agent.yaml:128-134`, `version-gate.yml:100`.
- H2's "17 topics" re-derived independently from the live root: 1 `inbox/bobi-eng-team-director` + 12 monitor keys (6 monitors x 2 forms) + 4 lifecycle keys = 17, against a 7-entry workspace layer, total 24. It is a snapshot of a mutable quantity, since `bobi/cli.py:2965`/`:2992`/`:3016` can change the monitor set, but the argument does not depend on the exact number.
- H5's arm ordering proven by execution: a `WorkspaceSubscriptionsError` hits arm 1, a `FileNotFoundError` still hits arm 2, a `yaml.YAMLError` still hits arm 3, and `issubclass(FileNotFoundError, WorkspaceSubscriptionsError)` is `False`, so nothing is shadowed. The exception reaches the arm because `bobi/ingress.py:83` calls `explicit_subscriptions` bare, with no try around it.
- Old Q1's closure is justified: `core.spec.ts:2301` is `expect(body.subscriptions).toEqual(["inbox/test", "slack:T1:app:A1"])` and `test_event_server.py:518` is `assert data["subscriptions"] == ["inbox/protocol-sync"]`, so D4's own condition ("if it already returns the accepted set") is met. One residual it does not state: the record is what the server ACCEPTED at T, not what it routes now, and `bobi/subagent.py:1978-1980` documents that the server-side index can go stale independently ("the deployment was dropped from the index during a long redeploy gap"), in which case `list` reports green while nothing is routed. The "last accepted at T" label conveys staleness but not that cause.

## NOT VERIFIED

- Whether the live event server actually holds the 24-topic set for `bobi-eng-team-director`. Reading it needs an authenticated call, which I did not make. The 17 and 24 are composed from the live root's `package/agent.yaml`, `MonitorRegistry.load`, and `manager_session_name`.
- Whether any of the 15 tests pass. I ran no test suite, only targeted probes.
- H7's chosen behavior end to end. I confirmed the spec states RESOLVED topics only at test 13 (line 462) and nowhere in the Design section, but I did not re-derive the `${}` exposure; round 3 verified no fleet pack uses `${}` inside `subscribe:`, so it stays latent.
- moda-agents citations other than the two on added lines (`agent.yaml:128-134`, `version-gate.yml:100`). Revision 4 changed no other moda-agents token, and round 3 verified the rest at `bf9d1088`.
