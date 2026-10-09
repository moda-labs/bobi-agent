# Review 2: Workspace-owned event subscriptions (revision 2)
Reviewer: independent, fresh context. Pinned at bobi-agent 83bebe49 / moda-agents bf9d1088 / spec c3796eca.

Pins verified:
```
$ for d in main-verify-1116 ma-verify-1116 952-subscriptions; do git -C /data/.bobi/agents/eng-team/run/worktrees/$d rev-parse HEAD; done
83bebe49af55523dec7c587aedfd7f6cbd205343
bf9d1088ad08bafd175232daeddcce3ab2d7e76c
c3796eca8ae14b6278741c6f153f091e3e8df697
$ git -C .../952-subscriptions diff --stat 83bebe49 HEAD -- plans/
 plans/2026-10-09-workspace-event-subscriptions.md | 443 ++++
 plans/reviews/2026-10-09-952-subscriptions-review-1.md | 637 ++++
```
The branch adds only the two markdown files, so every citation below was checked against untouched main.
No `gh`, no network, no edits outside `/tmp/952rev/`.

## Verdict

Not approvable as written, but much closer than revision 1.
The fold is real: 19 of round 1's 20 findings are genuinely fixed, the moda-agents citations are now all correct, and dropping `composed_subscriptions` removed the two blockers at their root rather than patching them.
The design is also smaller than revision 1 and the "edit what the server last accepted, never recompose" principle is sound.

The single worst remaining problem is G1, and it is new in revision 2.
Seeding the workspace file from the pack's `subscribe:` writes `subscribe: []` for every team whose pack has no `subscribe:` key, because `explicit_subscriptions` cannot tell "no key" from "empty list".
The new presence-not-truthiness gate then returns that `[]` as an authoritative "subscribe to nothing explicit", on the very first boot, before `discover_subscriptions` ever runs.
That is 5 of 7 moda-agents packs, 4 of them with `events: true` services, every team created by `bobi setup`, and both test fixtures that boot a manager.
The spec's "Migration: no behavior change on upgrade" and its test 14 both assert the opposite of what the code would do.

Three more defects would stop an implementer: G2 (the CLI rewrites the credential-bearing deployment record cross-process with no lock and no merge), G4 (`put_subscriptions` as specified cannot surface `400 unauthorized_topics`, so CLI step 9 is not implementable), and G5 (the record's `subscriptions` field is under-specified for absent-vs-empty, and getting it wrong permanently voids the deaf-reconnect repair PUT).
G1, G2, G4, G5, G6 and G7 are all consequences of the same choice: the record became load-bearing without the spec auditing the record's existing readers, writers and shape assertions.

Fix G1 through G7 and this is approvable.
Nothing in the design is wrong at the level revision 1 was.

## Round-1 fold audit

### Findings F1-F20

| # | Round-1 finding | Verdict | Evidence |
|---|---|---|---|
| F1 | `composed_subscriptions` omits `inbox/<session>` | **FIXED** | `grep -n composed_subscriptions <spec>` -> 0 hits. The function is gone; the CLI edits the recorded accepted set (spec 260-261), so `inbox/`, monitor and lifecycle topics pass through by construction. |
| F2 | deaf hook is every session's | **FIXED** | Spec 236-238 has `_resubscribe_on_deaf` read its OWN record. `grep -n "_start_event_subscription" bobi/` still shows only `session.py:1673` and `:1710`, so the per-session property holds. New risk is G5, not a leak. |
| F3 | malformed workspace YAML unspecified | **PARTIAL** | Spec 188-213 specifies raise + the outside-the-`try` placement and traces manager boot, `snapshot.py`, `service.py`. It misses `bobi/doctor.py:663-684`, a fourth reader (G8). |
| F4 | stale pin + wrong workflow citation | **FIXED** | `grep -n '^BOBI_VERSION' .github/fleet-version` -> `31:BOBI_VERSION=0.59.0`; spec 406 says 0.59.0. `deploy-agent-teams.yml:91`/`:177`, `team-images.yml:139`, `version-gate.yml:100` all confirmed; `release-fleet.yml:342` now correctly labelled the candidate install. |
| F5 | `authorize_resources` is a network write that fails open | **PARTIAL** | Spec 116-123 states the write and step 5 fails on an empty bubble (`state.py:79`, `server.py:713-714` both confirmed). Still wrong on scope: it writes grants only for `github:`/`linear:` (G6). |
| F6 | `filter_unauthorized=True` contradiction | **FIXED** | Spec 122-123 and 422 use `False` on the new path, matching `subagent.py:1883`. |
| F7 | ingress reads `explicit_subscriptions` | **FIXED** | Spec 108-114 and 291 put the workspace layer inside `explicit_subscriptions`. Verified both pins survive: `tests/test_ingress.py:260-276` writes no workspace file, and `:306-324` monkeypatches only `explicit_subscriptions`, so the new pre-`try` `workspace_subscriptions` call returns `None` and the fall-through still yields `[repo_path.name]`. |
| F8 | no `fsutil.file_lock` | **PARTIAL** | Spec 219-221 locks the workspace file and drops the false single-writer claim (`fsutil.py:162`, `AGENTS.md:97` confirmed). The deployment record, now a second cross-process read-modify-write surface, gets no lock (G2). |
| F9 | every moda-agents range wrong | **FIXED** | `grep -n '^subscribe:\|^managed_repos:' agents/moda-eng-team/agent.yaml` -> `127:`, `151:`; `wc -l` -> 162. Spec 18/331/332/333 all match, and 331 now says "through EOF ... as a block rather than a line range". |
| F10 | CLI silently re-syncs monitor topics | **FIXED** | Eliminated by not recomposing (spec 58-61). `cli.py:2965`, `:2992`, `:3016` and `service.py:631-633` confirmed; test 5 pins it. |
| F11 | `protocol` is optional | **FIXED** | Spec 267 matches `core.ts:37` (`if (!("protocol" in body)) return null;`). |
| F12 | wrong repo owner in the example | **FIXED** | Spec 166-172 lists all 7 topics identical to `agent.yaml:128-134`, `github:moda-labs/lightweave` included. |
| F13 | comment round-trip has no mechanism | **FIXED** | Spec 184-186. `grep -n yaml pyproject.toml` -> only `pyyaml>=6.0`; no ruamel. |
| F14 | es_url source and default unspecified | **FIXED** | Step 6 names the `http://localhost:8080` fallback and `ensure_running`/`local_port_from_url` (`server.py:393`, `:503` confirmed). Minor: the cited range should be `:1948-1955`, 1947 is blank. |
| F15 | test 7's red-state bar vacuous | **FIXED** | Rev-2 test 8 asserts on `discover_subscriptions`, which exists pre-change, so the pre-change red is substantive, not an `ImportError`. Sound. |
| F16 | three off-by-one ranges | **FIXED, but regressed elsewhere** | `:1894-1924` ✓, `core.ts:1537-1540` ✓, `active_subscriptions` list now includes `:1982` ✓. Revision 2 introduces new ones: G13 and G14. |
| F17 | "frozen read-only image" overbroad | **FIXED** | Spec 206-208 narrows it to `agent.yaml` and names the `monitors.yaml` exception. |
| F18 | docs change was a gesture | **FIXED** | Spec 322-326 names `EVENT_SERVER.md:378-398`, `BUILDING_AGENT_TEAMS.md:129-133`, `skills/bobi.md`, `AGENTS.md:133`. All four confirmed. |
| F19 | ROLE.md premise unverified | **FIXED** | Spec 148-149 says "D5's prompt half is an addition, not an update" and cites `ROLE.md:12`, `:22` and the now-false `agent.yaml:155`. All three confirmed verbatim. |
| F20 | baohua documents old precedence | **FIXED** | Spec 348-350 cites `agents/baohua/agent.yaml:105`, `:118`, `README.md:152`. All confirmed. |

### Cruft items C1-C5

| # | Round-1 ask | Verdict | Note |
|---|---|---|---|
| C1 | cut the cached accepted set | **NOT ADOPTED, defensibly** | C1's complaint was "nothing refreshes it". Revision 2 answers that: the two existing PUT/register sites write it and the deaf hook reads it, so it is load-bearing rather than decorative, and Q1 now weighs round 1's missing option (c). I do not relitigate it. But keeping it is the direct cause of G2, G3, G5 and G7, none of which the spec audited. |
| C2 | tests 1 and 3 duplicate existing coverage | **FIXED** | `grep -n unauthorized_topics event-server/test/core.spec.ts` -> only `:2030`, the register path, so rev-2 test 1 (the PUT path) is genuinely new. The two duplicates moved to "deliberately NOT new tests" with correct citations `:2284-2306` and `:2314-2324`. |
| C3 | delete the out-of-scope `BOBI_VERSION` bullet | **FIXED** | Gone. |
| C4 | compress S1's seven lines | **FIXED** | Two lines at spec 136-137. |
| C5 | prose repetition | **PARTIAL** | "no repo concepts in `bobi/`" still appears at spec 32 and 420. The N4 trap is now stated once (57-58) plus its test, which is fine. |

### Missing-coverage items

| Round-1 gap | Verdict |
|---|---|
| malformed-file policy | **PARTIAL** (G8) |
| `inbox/<session>` in the composition | **FIXED** (removed the recomposition; test 4 pins `bobi agent <name> message`) |
| per-session scoping of the deaf hook | **FIXED** in design, **test not buildable as categorized** (G12) |
| concurrency | **PARTIAL**: test 13 covers the workspace file only, not the record (G2) |
| `bobi/ingress.py` in the change list | **FIXED** (spec 291 changes the function ingress reads) |
| full add/remove diff | **FIXED** (step 9) |
| `skills/bobi.md` | **FIXED** (spec 325) |
| ROLE.md citations | **FIXED** (spec 345-346) |
| what `list` does when the manager is down | **NOT FIXED** (G10) |
| who runs the CLI | **FIXED** (spec 251-253; `cli.py:80-95` and `paths.py:92` confirmed, and the `agent` group does bind the root at `cli.py:452`) |

## New findings

### G1. Seeding from the pack writes `subscribe: []` for every team with no pack `subscribe:`, and the new presence gate then returns it, so the first boot after upgrade drops every auto-detected subscription  [severity: blocker]

**Claim:** Spec 217 seeds the workspace file from "the installed pack's `subscribe:` value", and spec 196-198 makes `[]` mean "the operator subscribes to nothing explicit, and is returned as such".
`explicit_subscriptions` returns `[]` both when the pack declares `subscribe: []` and when the pack declares no `subscribe:` key at all, so the seeder cannot distinguish them.
The seed therefore writes an authoritative empty list for every auto-detecting team, and because the seed call runs BEFORE the read at `service.py:625`, the damage lands on the very first boot, not the second.

**Evidence:**
```
$ cat /tmp/952probe/run/package/agent.yaml
agent: probe-team
entry_point: director
services:
  - name: github
    events: true
$ cd /tmp && PYTHONPATH=$BA_MAIN python3 /tmp/952probe/probe.py
module: .../main-verify-1116/bobi/events/subscriptions.py
explicit_subscriptions (the seed source): []
discover_subscriptions TODAY: ['run']
```
`explicit_subscriptions` also returns `[]` for an absent file (`bobi/events/subscriptions.py:41-42`) and for a non-dict document (`:44-45`).

Blast radius, derived mechanically rather than argued:
```
$ cd $MA_MAIN && for f in agents/*/agent.yaml; do echo "$(grep -c '^subscribe:' $f)  $f"; done
1  agents/baohua/agent.yaml
0  agents/gtm-team/agent.yaml
0  agents/market-research/agent.yaml
1  agents/moda-eng-team/agent.yaml
0  agents/roadmap-pm/agent.yaml
0  agents/support-manager/agent.yaml
0  agents/zachs-personal-assistant/agent.yaml
$ grep -ln "events: true" agents/*/agent.yaml
agents/moda-eng-team/agent.yaml  agents/baohua/agent.yaml  agents/market-research/agent.yaml
agents/roadmap-pm/agent.yaml  agents/gtm-team/agent.yaml  agents/support-manager/agent.yaml
$ cd $BA_MAIN && grep -rln "^subscribe:" --include=*.yaml --include=*.yml . | grep -v node_modules
(no output)
```
So 5 of 7 fleet packs have no `subscribe:`, 4 of those publish `events: true` services, and bobi-agent ships no `agent.yaml` template with `subscribe:` at all.
The repo's own docs call that the preferred shape: `docs/BUILDING_AGENT_TEAMS.md:129-130` reads "explicit subscription override - rarely needed; omitting it enables auto-detection (preferred)".

Both test fixtures that boot a manager are in exactly this state:
```
$ sed -n '277,283p' tests/conftest.py      # bobi_install
        "services": [ {"name": "slack", "events": True} ],   # no subscribe:
$ sed -n '96,107p' tests/integration/conftest.py
    agent_yaml = { ... "services": [ {"name": "github", "events": True} ], ... }   # no subscribe:
```

**Why it matters:** `market-research`, `roadmap-pm`, `gtm-team` and `support-manager` go deaf to every GitHub, Slack and Linear topic on their first boot after the upgrade, silently, with only an inbox, monitor and lifecycle subscription left.
`_sync_saved_deployment` then PUTs that reduced set with `replace`, so `core.ts:1522-1530` removes the real topics from the live index.
This also falsifies three of the spec's own statements: 393 ("No behavior change on upgrade"), 396 ("the composed list is identical to what the same boot would have produced before"), and test 14.
It would be caught by CI, because `tests/integration/test_e2e_event_flow.py` boots a real manager against the fixture pack above and depends on a live `github:` topic, but a Gate-1 approver is being asked to accept a migration argument that is false.

**Fix:** Make the seed write-if-absent AND non-empty: skip seeding entirely when the pack's `subscribe:` is empty or absent, so an auto-detecting team keeps no workspace file and keeps auto-detecting until an operator or the CLI creates one.
State that consequence explicitly: the workspace file exists only for teams that declared a seed or ran `subscriptions add`, and `subscriptions add` on a team with no file creates it from the live accepted set, not from `[]`.
Restate Migration and test 14 against an auto-detecting pack, not only against eng-team.

### G2. The CLI rewrites the credential-bearing deployment record whole, cross-process, with no lock and no merge  [severity: blocker]

**Claim:** CLI step 8 (spec 268) records the response's `subscriptions` and a timestamp "on the manager's session record".
That record is the same file holding the session's live `deployment_id` and `api_key`, `save_deployment_state` writes the document whole, and the spec takes `file_lock` only around the workspace YAML (step 2).

**Evidence:**
```
$ sed -n '55,64p' $BA_MAIN/bobi/events/state.py
def save_deployment_state(project_path, session, deployment_id, api_key) -> None:
    state_file = deployment_state_path(project_path, session)
    atomic_write_json(state_file, {
        "deployment_id": deployment_id,
        "api_key": api_key,
    }, indent=None)
```
There is no lock, no read-before-write, and no field preservation: every call replaces the whole document.
A register supersedes the prior deployment by name and deletes it:
```
$ sed -n '1445,1451p' $BA_MAIN/event-server/core/src/core.ts
	const prior = await storage.getDeploymentByName(name, bubble.id);
	if (prior) { for (const sub of prior.subscriptions) { await storage.removeSubscription(...); }
		await storage.removeDeployment(prior); }
$ sed -n '156,157p' $BA_MAIN/bobi/service.py          # clear_manager_session
    for sub in ("deployments", "cursors"):
        shutil.rmtree(paths.state_path(project_path) / sub, ignore_errors=True)
```
`AGENTS.md:96-98` is explicit that this shape needs the lock: "Read-modify-write state (a load, a mutate, a save) additionally takes `fsutil.file_lock`; atomicity alone keeps the file parseable but does not stop a concurrent updater's change from being overwritten."

**Why it matters:** Two concrete losses.
First, a `subscriptions add` that reads the record and then writes it after a concurrent `clear_manager_session` plus re-register puts the OLD `deployment_id`/`api_key` back over the new ones.
The next manager start takes the `_sync_saved_deployment` branch (`subagent.py:1957-1960`), PUTs to a deployment the server deleted, gets 403, raises, and `session.py:1681-1688` boots the manager anyway into a background retry loop that re-reads the same stale record forever.
The manager runs with no event client and no error that names the cause, permanently, until someone deletes the file by hand.
Second, two concurrent `add`s can interleave as read-A, read-A, PUT-1, PUT-2, write-2, write-1, leaving the record holding PUT-1's set while the server holds PUT-2's; test 13 covers only the workspace file, so nothing catches it.

**Fix:** Say in step 8 that the record write takes `fsutil.file_lock(deployment_state_path(project_path, session))`, re-reads the record inside the lock, and merges only `subscriptions` and `subscriptions_at`, failing loudly if `deployment_id` changed since the read.
Say that `_register_with_retry` (`:1834`) and the new sync-site write (`:1891`) take the same lock.
Either change `save_deployment_state` to a merging update, or add a separate `record_accepted_subscriptions(project_path, session, subscriptions)` that never touches the credential fields.
Add a test: an `add` whose record write races a re-register must not revert `deployment_id`.

### G3. `register()` returns no subscription set, so "record the returned set at `:1834`" is not implementable, and the field would mean two different things  [severity: major]

**Claim:** Spec 303 defines `put_subscriptions(...) -> list[str]` "that PUTs and returns the server's reported `subscriptions`", and spec 308 says "Record the returned set via `save_deployment_state` at `:1834` (register) and after the PUT at `:1891`".
Spec 228 calls the field "the set the server confirmed", spec 79 calls `:1834-1835` and `:1891-1892` "Both PUT sites", and spec 231 calls them "Both existing PUT/register sites".
At `:1834` nothing was PUT and nothing set-shaped was returned.

**Evidence:**
```
$ sed -n '849,875p' $BA_MAIN/bobi/events/server.py
def register(base_url, name, subscriptions, bubble_id="", bubble_key="", _retry_unauthorized=True) -> tuple[str, str]:
    """JOIN a deployment into the instance's bubble. Returns (deployment_id, api_key)."""
    ...
    return result["deployment_id"], result["api_key"]
$ sed -n '1473,1482p' $BA_MAIN/event-server/core/src/core.ts
	const resp: Record<string, unknown> = {
		deployment_id: deploymentId,
		api_key: apiKey,
		bubble_id: bubble.id,
		protocol: EVENT_PROTOCOL,
	};
	if (minting) resp.bubble_key = bubble.key;
	return { status: 201, body: resp };
```
The register response body has no `subscriptions` member, and `register()` discards everything except the two strings.
The two paths also differ in substance: `handleUpdateSubscriptions` de-duplicates (`core.ts:1518`, `[...new Set(replaceSubs)]`) while `handleRegisterDeployment` stores the request array verbatim (`core.ts:1461`).

**Why it matters:** An implementer following spec 308 looks for a return value that does not exist and will either change `register()`'s signature (out of the spec's change list) or quietly record something else.
The honest thing to record at the register site is the `authorized` list already in hand at `:1835`, which a 201 proves the server stored verbatim.
That is correct, but it means the field holds a client-asserted set at one site and a server-echoed set at the other, and the reader (the deaf hook, and `list`'s staleness label) is specified against the server-echoed meaning only.

**Fix:** Rewrite 79, 228, 231 and 308: the register site records the `authorized` list it passed to `register()`, justified by the server's reject-as-a-unit guarantee (`core.ts:1436-1439`) and the verbatim store at `:1461`; `put_subscriptions`'s return value is used only at the sync site and in the CLI.
Drop "Both PUT sites" and "the set the server confirmed" in favour of "the set the server accepted".

### G4. `put_subscriptions` as specified cannot surface `400 unauthorized_topics`, so CLI step 9 is not implementable, and adding the raise degrades the boot-path error message  [severity: major]

**Claim:** CLI step 9 (spec 270): "On a `400 unauthorized_topics`, print the topics the server named and state that nothing was applied live."
The closure the spec moves (`subagent.py:1926-1946`) has no `unauthorized_topics` branch, and spec 303 specifies the new helper only as "PUTs and returns the server's reported `subscriptions`".

**Evidence:**
```
$ sed -n '1938,1946p' $BA_MAIN/bobi/subagent.py
    try: data = resp.json()
    except ValueError: data = None
    raise_for_protocol_error(resp.status_code, data)
    resp.raise_for_status()
    if not isinstance(data, dict) or "error" in data:
        raise ValueError("Unexpected subscription reply")
    validate_server_response(data)
$ sed -n '838,842p' $BA_MAIN/bobi/events/server.py      # the register path DOES parse it
    if resp.status_code == 400:
        if isinstance(data, dict) and data.get("error") == "unauthorized_topics":
            raise UnauthorizedTopics(list(data.get("topics") or []))
$ grep -rn "UnauthorizedTopics" --include=*.py $BA_MAIN/bobi/
bobi/events/server.py:627:class UnauthorizedTopics(Exception):
bobi/events/server.py:842  bobi/events/server.py:864
```
So today the PUT's 400 surfaces as an `httpx.HTTPStatusError` from `raise_for_status()`, with the topic list thrown away.
Adding the `UnauthorizedTopics` raise to the shared helper changes the boot path too:
```
$ sed -n '1899,1916p' $BA_MAIN/bobi/subagent.py
        if isinstance(e, httpx.HTTPStatusError):
            status = e.response.status_code
            ...
            elif status == 400:
                reason = "subscription configuration or resource grants rejected (HTTP 400)"
        ...
        else:
            reason = "unexpected subscription failure; check server configuration"
```
`UnauthorizedTopics` is a plain `Exception` (`:627`), so it misses the `HTTPStatusError` branch and the operator's boot message degrades from "resource grants rejected (HTTP 400)" to "unexpected subscription failure".

**Why it matters:** Step 9 is the whole operator-facing value of the mutation path, and the design's answer to D4 ("a 400 naming the exact topics", spec 131) depends on it.
As written the helper cannot produce the topic list, and the obvious fix silently regresses an existing diagnostic that `tests/test_resource_grants.py:322-330` cares about on the register path.

**Fix:** Spell out the helper's full contract in spec 303: the pooled client (`bobi/http.put`, `:78`), `timeout=10.0`, `{"replace": [...], "protocol": protocol_payload()}`, `raise_for_protocol_error`, a new `UnauthorizedTopics` raise on `400 {"error":"unauthorized_topics"}`, `raise_for_status`, the `"error" in data` guard, `validate_server_response`.
Add one line to the `bobi/subagent.py` change list: extend `_sync_saved_deployment`'s classifier with an `UnauthorizedTopics` branch that names the topics, so the boot message does not regress.

### G5. The recorded `subscriptions` field is under-specified for absent-versus-empty, and getting it wrong permanently voids the deaf-reconnect repair PUT  [severity: major]

**Claim:** Spec 236 has the deaf hook fall back to `active_subscriptions` "when the record has no `subscriptions` field".
Nothing says what `put_subscriptions` returns when the response body omits `subscriptions`, nor that an empty result must not be recorded.

**Evidence:** the response is not required to carry the field.
```
$ sed -n '55,70p' $BA_MAIN/bobi/events/protocol.py
def validate_server_response(payload) -> dict[str, int]:
    """... A missing ``protocol`` member is the legacy v1 response shape.
    Unknown fields are ignored so additive changes remain compatible."""
```
`subscriptions` is never checked, and the existing PUT guard only rejects a non-dict or an `"error"` key.
Four existing tests answer the PUT with exactly `{"ok": True}`:
```
$ grep -n 'json={"ok": True}' $BA_MAIN/tests/test_event_subscription.py
119:        return httpx.Response(200, json={"ok": True})
348:        return httpx.Response(200, json={"ok": True})
456:        return httpx.Response(200, json={"ok": True})
499:        return httpx.Response(200, json={"ok": True})
```
If `put_subscriptions` returns `data.get("subscriptions", [])` and the caller records `[]`, the next deaf reconnect PUTs `{"replace": []}`:
```
$ sed -n '1498,1500p' $BA_MAIN/event-server/core/src/core.ts
	if (replaceSubs !== undefined && !replaceSubs.length) {
		return { status: 400, body: { error: "replace[] must not be empty" } };
	}
$ sed -n '518,519p' $BA_MAIN/bobi/events/client.py
        except Exception as e:
            log.debug("Resubscribe after deaf reconnect failed: %s", e)
```
Every subsequent repair PUT fails identically, and the spec's own `debug` -> `warning` change (spec 315) is the only trace.

**Why it matters:** The repair PUT is the mechanism that makes a CLI change survive a reconnect, which is the reason the record exists at all (spec 236-238).
An `[]` written once makes it fail forever, on a daemon thread, with the exception swallowed.
The same ambiguity hits `list`: `[]` recorded means "the server routes nothing", which is never true, since the live set always contains `inbox/<session>` (spec 128).

**Fix:** State that `put_subscriptions` returns `None` when the response has no `subscriptions` array, that an absent or empty result is NOT recorded, and that the record reader treats a missing OR empty `subscriptions` as "no accepted set" and falls back to `active_subscriptions`.
Add to the test plan: a PUT whose 200 body omits `subscriptions` leaves the record's previous value intact and the next deaf reconnect still replays a non-empty set.

### G6. `authorize_resources` writes grants only for `github:` and `linear:`, so step 5's justification is false for three of the five global topic families  [severity: major]

**Claim:** Step 5 (spec 262): "For `add` only, call `authorize_resources(..., filter_unauthorized=False)` so a newly added global topic has its grant written before the PUT."

**Evidence:**
```
$ sed -n '640p' $BA_MAIN/bobi/events/server.py
_RESOURCE_CRED_KEYS = {"github": ("github", "token"), "linear": ("linear", "api_key")}
$ sed -n '761,765p' $BA_MAIN/bobi/events/server.py
        if service not in _RESOURCE_CRED_KEYS:
            # Non-global, or slack/whatsapp/discord (granted via their
            # registrations).
            kept.append(sub)
            continue
```
A `slack:`, `whatsapp:` or `discord:` topic is kept with no grant written; its grant comes from `register_slack_workspaces` / `register_whatsapp_numbers` / `register_discord_apps`, which only `_register_channel_credentials` (`subagent.py:1767`) calls and which step 5 does not.
The server checks all five prefixes:
```
$ sed -n '411p;1919,1927p' $BA_MAIN/event-server/core/src/core.ts
const GLOBAL_TOPIC_PREFIXES = ["github:", "linear:", "slack:", "whatsapp:", "discord:"];
		if (!isGlobalTopic(sub)) continue;
		...
		if (!(await storage.hasResourceGrant(parsed.service, parsed.resource, bubbleId))) { bad.push(sub); }
```
The slack grant is keyed on the TEAM id, not the channel (`core.ts:445-450`), so adding a channel inside an already-registered workspace does pass.
Adding a topic for a NEW slack team, or any `whatsapp:`/`discord:` topic, always 400s.

**Why it matters:** D3 is deliberately generic about topics.
As written, `subscriptions add` can apply live for `github:`, `linear:` and in-workspace `slack:` only; for the rest it always fails the live apply while persisting the workspace change.
That is not a silent failure (step 9 reports the 400), but the spec states the opposite as a property, and an implementer will not discover the three-family gap until a test fails.

**Fix:** Either narrow the claim in step 5 and say so in "Out of scope" ("a `whatsapp:`/`discord:`/new-workspace `slack:` topic persists and applies at next boot, because its grant is written by the channel registration the manager runs at startup"), or have step 5 call `_register_channel_credentials` too and pass `whatsapp_registered`/`discord_registered` through, as `_sync_saved_deployment` does at `subagent.py:1876-1886`.

### G7. Three existing exact-JSON-shape assertions on the deployment record break, and `tests/test_event_subscription.py` is in neither the change list nor the test plan  [severity: major]

**Claim:** Spec 153 says "one new field on an existing state record" and spec 300 says the extension preserves "the existing two fields and the atomic write".
The record's shape is asserted by exact equality in three existing tests.

**Evidence:**
```
$ sed -n '70,71p' $BA_MAIN/tests/test_event_subscription.py      # register path
    saved = json.loads(_state_file(project).read_text())
    assert saved == {"deployment_id": "dep-1", "api_key": "key-1"}
$ sed -n '469,471p' $BA_MAIN/tests/test_event_subscription.py    # after a successful sync PUT
    # The saved deployment survives — nothing was re-minted.
    assert json.loads(state.read_text()) == {
        "deployment_id": "dep-L", "api_key": "key-L"}
$ sed -n '514,515p' $BA_MAIN/tests/test_event_subscription.py    # after a successful sync PUT
    assert json.loads(state.read_text()) == {
        "deployment_id": "dep-L", "api_key": "key-L"}
```
Recording at `:1834` breaks the first; recording after the PUT at `:1891` breaks the other two, and that site does not call `save_deployment_state` today at all, so it is a new write where a test asserts none happened.
The spec's "Exact changes per file" lists no test file, and the test plan mentions only `tests/test_service.py:21-26`.

For the record, I checked the other readers and they are all safe: `load_deployment_state` reads by key (`state.py:50`), `bobi/service.py:384` uses `.get("deployment_id")`/`.get("api_key")` only, `bobi/doctor.py:470-471` globs filenames without parsing, `tests/test_bubble.py:169-188` asserts single keys, and `grep -rn "subscription" bobi/webapp/*.py` returns nothing so no web surface reads this record.

**Fix:** Add `tests/test_event_subscription.py` to the change list and say the three asserts become subset assertions (`saved["deployment_id"] == ...` plus `saved["subscriptions"] == [...]`), and that `:469-471`/`:514-515` must additionally pin that `deployment_id` and `api_key` are UNCHANGED, which is the property their docstrings care about.

### G8. `bobi doctor` is a fourth reader of `explicit_subscriptions`, and it reports a malformed workspace file as `ok=True`  [severity: major]

**Claim:** Spec 210-213: "The two other readers already degrade safely", naming `bobi/supervisor/snapshot.py:105-108` and `bobi/service.py:183-194`.
There is a third, and it is the command an operator runs to find exactly this problem.

**Evidence:**
```
$ grep -rn "check_ingress_reachability" --include=*.py $BA_MAIN/bobi/
bobi/doctor.py:51:    results.append(_check_ingress_reachability())
bobi/doctor.py:670,672   bobi/ingress.py:67   bobi/service.py:185,187
$ sed -n '669,678p' $BA_MAIN/bobi/doctor.py
    try:
        from bobi.ingress import check_ingress_reachability
        warning = check_ingress_reachability(root)
    except FileNotFoundError:
        return CheckResult("Ingress reachability", ok=True, detail="no agent config")
    except Exception as exc:
        return CheckResult("Ingress reachability", ok=True, detail=f"skipped: {exc}")
```
`check_ingress_reachability` calls `explicit_subscriptions` at `ingress.py:83`, which revision 2 makes raise on a malformed workspace file.
So `bobi doctor` renders "Ingress reachability: ok - skipped: <path>: <yaml error>".

**Why it matters:** The design's whole argument for the presence/raise redesign is that `<run>/workspace/` is writable, agent-edited, and must fail LOUD (spec 103-106).
A malformed file does fail the manager boot, but the diagnostic command reports green, with the real cause buried in a `detail` string on a passing check.
An operator debugging "my team went deaf after editing subscriptions.yaml" runs `doctor` first.

**Fix:** Add `bobi/doctor.py:663-684` to the malformed-file trace, and add a change-list entry: a malformed workspace subscriptions file makes the ingress check FAIL (`ok=False`) with the path, rather than falling into the `ok=True, skipped:` arm.
Add it to test 9's assertions alongside the `snapshot.py` case.

### G9. `remove`-skips-authorization is correct, but the stated reason is wrong and the real invariant is unstated  [severity: minor]

**Claim:** Step 5 (spec 263): "`remove` skips it: removing a topic needs no grant, and calling it would re-POST credentials for every remaining topic."
The first half is not the reason it is safe.

**Evidence:** the server re-checks EVERY global topic in the `replace` array, including survivors.
```
$ sed -n '1504,1512p' $BA_MAIN/event-server/core/src/core.ts
	const newSubs = replaceSubs !== undefined ? replaceSubs : addSubs!;
	const unauthorized = await unauthorizedGlobalTopics(storage, deployment.bubble_id, newSubs);
	if (unauthorized.length) { return { status: 400, body: { error: "unauthorized_topics", topics: unauthorized } }; }
```
What makes it safe is that grants are permanent.
```
$ sed -n '147,165p' $BA_MAIN/event-server/worker/src/index.ts
		async putResourceGrant(grant) { await env.EVENTS.put(`resource_grant:...`, JSON.stringify(grant)); ... }
		async hasResourceGrant(service, resource, bubbleId) { const data = await env.EVENTS.get(...); return data !== null; }
$ grep -rn "removeResourceGrant\|revokeResourceGrant" $BA_MAIN/event-server/core/src/core.ts
(no output)
```
No `expirationTtl`, no revocation route anywhere in `core.ts`, so a grant written at an earlier boot is still valid at `remove` time.

**Why it matters:** The reason as stated ("removing a topic needs no grant") would equally justify skipping authorization on `add`, which is wrong.
The invariant the design actually relies on is unstated, so a future change that adds grant expiry or revocation would break `remove` with nothing pointing at it.

**Fix:** Restate: "a `remove` re-asserts every surviving topic and the server re-checks them all (`core.ts:1509`), but resource grants are written without a TTL and have no revocation route, so a survivor granted at an earlier boot still passes."
Add one assertion to the test plan: a `remove` whose survivors include a `github:` topic granted at a prior boot returns 200 with no re-authorization.

### G10. `list` has no specified behavior when no deployment record exists  [severity: minor]

**Claim:** Spec 276-278 specifies `list` only for the case where a record with an accepted set exists.
Spec 272-274 covers the absent-record and no-`subscriptions`-field cases for `add` and `remove`, and never extends them to `list`.
This was a round-1 missing-coverage item and is not folded.

**Evidence:** `load_deployment_state` returns `{}` for an absent file (`$BA_MAIN/bobi/events/state.py:47-48`), which is the normal state on a stopped team, and `list` is the command an operator runs first.

**Fix:** One sentence: with no record, or a record with no accepted set, `list` prints the persisted workspace list, says no live set is recorded and why (manager not running, or started before this version), and exits zero.

### G11. No statement on the state format version  [severity: minor]

**Claim:** The change adds fields to a persisted per-session state document, and the repo has an explicit versioning contract for exactly that.

**Evidence:**
```
$ sed -n '25,27p' $BA_MAIN/bobi/state_version.py
# Bump this when the on-disk state layout changes in a way that older
# code cannot safely ignore.
CURRENT_FORMAT_VERSION = 1
$ sed -n '13,15p' $BA_MAIN/bobi/state_version.py
- **Newer (on-disk > code):** refuse to start.
```
No bump is needed here, and I verified why: `load_deployment_state` reads by key, and an older `save_deployment_state` drops unknown fields on its next write without error, so a downgrade degrades to the `active_subscriptions` fallback.

**Why it matters:** `ensure_state_version` is called on the manager boot path (`bobi/service.py:648`), one line from the seed call the spec adds, and a bump would refuse to start on downgrade. A Gate-1 approver should not have to derive the answer.

**Fix:** One sentence under "The accepted set, recorded per session": no `CURRENT_FORMAT_VERSION` bump, because the fields are optional and an older bobi both ignores them on read and drops them on write.

### G12. Tests 6 and 7 are categorized as integration against the real local event server, but nothing in the repo can force a deaf reconnect there  [severity: major]

**Claim:** Spec 366-367 lists "force the worker's deaf reconnect" and "force the manager's deaf path" under "Integration, against the real local event server".

**Evidence:** the deaf path is entered only by the heartbeat detector, after 95 seconds of no pong.
```
$ grep -n "_needs_resubscribe" $BA_MAIN/bobi/events/client.py
229 (init)   297 (set True)   440,441 (consumed)
$ sed -n '294,297p' $BA_MAIN/bobi/events/client.py
            if since is not None and since > self._HEARTBEAT_TIMEOUT_S:
                self._deaf_reconnects += 1 ; self._connected.clear() ; self._needs_resubscribe = True
$ grep -n "_HEARTBEAT_TIMEOUT_S =" $BA_MAIN/bobi/events/client.py
260:    _HEARTBEAT_TIMEOUT_S = 95.0
```
The only harness that forces it is a fake WebSocket server whose connection #1 never pongs (`tests/test_event_client_heartbeat.py:78-79`, `:144-165`), which is a unit fixture, not the real event server.
Every existing deaf-hook test instead pulls the callback out of a MOCKED client:
```
$ grep -rn "on_deaf_reconnect" $BA_MAIN/tests/
tests/test_event_subscription.py:126   :318   :355
```
And no integration test boots a manager through the event-server fixture:
```
$ grep -rln "launch_team\|start_team" $BA_MAIN/tests/integration $BA_MAIN/tests/e2e
tests/integration/test_feedback_rca.py  test_manager_lifecycle.py  test_team_instructions.py
```
none of which use the `event_server` fixture or touch the deaf path.

**Why it matters:** Tests 2, 3, 4, 5 are buildable: `tests/integration/test_e2e_event_flow.py` already boots a real manager with a real event server and publishes topics, and `tests/integration/test_event_isolation.py:101-191` already runs two real session subprocesses against one.
Tests 6 and 7 are not, as categorized.
A test plan that silently requires new fixtures is how a feature ships with its most important regression test quietly downgraded.

**Fix:** Reclassify 6 and 7 as unit tests on the existing seam (`on_deaf_reconnect` from the patched client, as `tests/test_event_subscription.py:110-136` already does for the filtered-register case), and keep the cross-session isolation half as an integration test on the `test_event_isolation.py` harness without the deaf trigger.
If an integration-level deaf test is wanted, name the fixture it needs and the knob that shortens `_HEARTBEAT_TIMEOUT_S`.

### G13. `core.ts:1531-1536` does not contain the removal loop the spec attributes to it  [severity: minor]

**Claim:** Spec 54: "`event-server/core/src/core.ts:1531-1536` assigns `deployment.subscriptions = desired` and removes every stored subscription not in `desired`."

**Evidence:**
```
$ awk 'NR>=1522 && NR<=1536' $BA_MAIN/event-server/core/src/core.ts
1522: for (const sub of deployment.subscriptions) {
1523:   if (!desiredSet.has(sub)) { await storage.removeSubscription(...); removed++; }   # :1522-1530
1531: for (const sub of desired) { if (!deployment.subscriptions.includes(sub)) added++;
1533:   await storage.addSubscription(...); }                                            # :1531-1534
1535: deployment.subscriptions = desired;
1536: await storage.putDeployment(deployment);
```
The removal loop is `:1522-1530`, outside the cited range; `:1531-1534` is the ADD loop.
Round 1's spec cited `:1517-1536`, which was correct; revision 2 narrowed it wrongly.

**Why it matters:** This is the citation behind "`{"replace": [...]}` is authoritative", the fact the whole design turns on.
Spec line 7 makes exactness the spec's own contract.

**Fix:** Cite `:1517-1540`, or `:1522-1530` for the removal and `:1535` for the assignment.

### G14. Six off-by-one ranges, all new or carried in revision 2  [severity: minor]

**Claim and evidence**, each checked with `sed -n`:
- Spec 228 and 300: `save_deployment_state` at `bobi/events/state.py:55-66`. The function body ends at `:64`; `:65-66` are blank.
- Spec 77: the per-session record machinery at `state.py:32-66`. Same tail problem; `:32-64` is exact.
- Spec 211 and 376: `bobi/supervisor/snapshot.py:105-108` "wraps its call in `except Exception: pass`". `:104` is the `try`, `:108` is the `except`, and the `pass` is at `:109`.
- Spec 212: `bobi/service.py:183-194` wraps `check_ingress_reachability`. `:183` is `ingress_hint = ""`, the `try` is `:184`, and the `log.debug` is `:195`.
- Spec 265: "exactly as `bobi/subagent.py:1947-1955` does". `:1947` is blank; the block is `:1948-1955`.
- Spec 121: the `authorize_resources` docstring reason at `bobi/events/server.py:707-712`. The paragraph starts at `:706`; `:712` is the closing `"""`.

**Why it matters:** Individually trivial. Collectively they are the same class of error round 1 flagged as F16, and spec line 7 promises exactness, so they cost the document trust it needs at Gate 1.

**Fix:** Correct all six.

### G15. `ensure_running` makes a mutation command able to spawn a local event server  [severity: minor]

**Claim:** Step 6 (spec 265) calls `ensure_running` when `local_port_from_url` matches.

**Evidence:**
```
$ grep -n "^def ensure_running" $BA_MAIN/bobi/events/server.py
503:def ensure_running(port: int, webhook_secret=None, ..., project_path=None, extra_env=None) -> str:
    """Start the local event server if not already running."""
```
On a stopped team with a local URL, `subscriptions add` would start an event server process, then find no deployment record, then take the "nothing applied live" branch (spec 272-274) and exit zero, leaving the server running.

**Why it matters:** Small, but it makes a persist-only command leave a process behind, and D1 means the caller is often the director agent inside the runtime.

**Fix:** Order step 3 before step 6: resolve the deployment record first, and skip the URL/`ensure_running` work entirely when there is nothing live to update.

### G16. The double read of `workspace_subscriptions` is harmless except for one narrow race  [severity: nit]

**Claim:** Spec 202 has `discover_subscriptions` call `workspace_subscriptions` before the `try`, and spec 291 has `explicit_subscriptions` call it again inside.

**Evidence:** when the file is absent the outer call returns `None`, control enters the `try`, and `explicit_subscriptions` repeats one `Path.exists()`; when it is present the outer call returns early and the inner one never runs.
So the redundancy costs one stat, not a correctness problem.
The one gap: if the file is CREATED between the two reads it is read from inside the `try` at `bobi/events/subscriptions.py:59-66`, and if that newly created file is malformed the exception is swallowed into auto-detection, which is the exact outcome the placement exists to prevent.
The CLI writes the file with `atomic_write_text` (`fsutil.py:100-140`, rename-over), so the window needs a create, not a torn write, and it is a single boot.

**Fix:** Optional. One sentence noting the window, or have `discover_subscriptions` pass its already-read result into `explicit_subscriptions` so there is literally one read.

### G17. House style: seven bullets and one decision pack two sentences onto one line  [severity: nit]

**Evidence:**
```
$ grep -c '—' <spec>
0
$ grep -nE '[a-z)`"]\. +[A-Z`]' <spec>
32, 420, 421, 422, 423, 424, 425, 426
```
Zero em dashes, so that rule is clean.
Line 32 (D3) and the seven "Out of scope" bullets at 420-426 each carry two sentences on one physical line, against the house rule to keep one sentence per line and not wrap bullet sentences together.
Everything else in the file complies.

## Citation audit

77 distinct file:line claims checked. 69 CONFIRMED, 7 WRONG-LINE or MISDESCRIBED, 1 structurally false (G3). 0 NOT-FOUND.
Every bobi-agent and event-server claim was checked in `$BA_MAIN`, every moda-agents claim in `$MA_MAIN`, with `awk 'NR>=a && NR<=b'` or `sed -n`/`grep -n` as shown.

### bobi-agent: CONFIRMED

`bobi/events/subscriptions.py` `:13`, `:25`, `:28`, `:43-48`, `:46-48`, `:51`, `:59-66`, `:61`, `:68-78`.
`bobi/events/state.py` `:21-25`, `:44-52`, `:79`.
`bobi/events/server.py` `:650-655`, `:680-684`, `:713-714`, `:781-784`, `:849-875`.
`bobi/events/client.py` `:501-502`, `:518-519`.
`bobi/events/adapters.py` `:139-142`, `:290-315`.
`bobi/subagent.py` `:1715`, `:1761`, `:1765`, `:1805-1808`, `:1812`, `:1834`, `:1835`, `:1838`, `:1859-1864`, `:1867`, `:1883`, `:1891`, `:1892`, `:1894-1924`, `:1917-1920`, `:1926-1946`, `:1931`, `:1938-1946`, `:1974-1982`, `:1982`, `:1990`.
`bobi/session.py` `:1657-1660`, `:1673`, `:1710`.
`bobi/service.py` `:135-144`, `:384`, `:625`, `:625-642`, `:759`.
`bobi/ingress.py` `:83`. `bobi/paths.py` `:92`. `bobi/fsutil.py` `:100`, `:162`.
`bobi/install.py` `:174-192`. `bobi/runtime_guard.py` `:143-153`. `bobi/prompts/resolver.py` `:86-97`.
`bobi/cli.py` `:80-95`, `:2965`, `:2992`, `:3016`, `:3155`, `:4168`, `:4177-4179`.
`event-server/core/src/core.ts` `:37`, `:1498-1500`, `:1508-1512`, `:1537-1540`, `:1539`.
`docs/EVENT_SERVER.md:378-398` and `:396-398`. `docs/BUILDING_AGENT_TEAMS.md:129-133`. `AGENTS.md:12`, `:97`, `:133`.
`tests/test_ingress.py:260-276` and `:306-324`. `tests/test_symmetric_node.py:90-102`. `tests/test_service.py:21-26`.
`tests/test_memory.py:52,55,62,66`. `tests/integration/test_event_isolation.py:1-16`.
`tests/integration/test_event_server.py:62`, `:371`, `:394`, `:510-520`.
`event-server/test/core.spec.ts:2030`, `:2284-2306`, `:2314-2324`.
`grep -rn "managed_repos" bobi/` -> `exit=1`, confirmed.
The measured `_detect_github` probe at spec 93-101, re-run from outside any checkout so the local `bobi/` could not shadow it:
```
$ cd /tmp && PYTHONPATH=$BA_MAIN python3 -c "import bobi.events.adapters as a; from pathlib import Path; print(a.__file__); print(a._detect_github(Path('/data/.bobi/agents/eng-team/run'), None))"
.../main-verify-1116/bobi/events/adapters.py
['github:moda-labs/bobi-agent']
```
CONFIRMED, and the "4 GitHub topics collapse to 1" claim holds.

### bobi-agent: WRONG-LINE or MISDESCRIBED

| Spec line | Citation | Verdict | Correction |
|---|---|---|---|
| 54 | `core.ts:1531-1536` "removes every stored subscription not in `desired`" | **MISDESCRIBED** | removal is `:1522-1530`; see G13 |
| 77 | `bobi/events/state.py:32-66` | WRONG-LINE | `:32-64` |
| 211, 376 | `bobi/supervisor/snapshot.py:105-108` `except Exception: pass` | WRONG-LINE | `pass` is `:109` |
| 212 | `bobi/service.py:183-194` | WRONG-LINE | `:184-195` |
| 228, 300 | `bobi/events/state.py:55-66` | WRONG-LINE | `:55-64` |
| 265 | `bobi/subagent.py:1947-1955` | WRONG-LINE | `:1948-1955` |
| 121 | `bobi/events/server.py:707-712` | WRONG-LINE | `:706-711` |
| 231, 308 | `:1834-1835` / `:1891` as "both PUT sites" recording "the returned set" | **STRUCTURALLY FALSE** | `register()` returns no set; see G3 |

### moda-agents: all CONFIRMED

`agents/moda-eng-team/agent.yaml` `:103-126`, `:124-126`, `:127-134`, `:128-134`, `:151`, `:151-162`, `:155`, EOF at `:162` (`wc -l` = 162).
`scripts/check-deploy-compose.py:76-83` and `:85-94`.
`agents/moda-eng-team/roles/director/ROLE.md:12` ("from configuration") and `:22` ("in configuration").
`agents/baohua/agent.yaml:105`, `:118`; `agents/baohua/README.md:152`.
`.github/fleet-version` -> `31:BOBI_VERSION=0.59.0`.
`.github/workflows/deploy-agent-teams.yml:91`, `:177`; `team-images.yml:139`; `version-gate.yml:100`; `release-fleet.yml:342` (correctly described as the candidate install, not the pin).
Four packs ship `workspace/`: `gtm-team`, `market-research`, `support-manager`, `zachs-personal-assistant` (`for d in agents/*/; do [ -d "$d/workspace" ] && echo "$d"; done`), and `agents/moda-eng-team/` has none.

### Shorthand ambiguity

The brief asked me to flag genuinely ambiguous basenames.
One case: `core.ts:37` (spec 267) is the only citation in the CLI section that uses a bare TypeScript basename, five sections after the last full path at spec 54.
Everything else resolves cleanly from context, including both `agent.yaml` senses.

## Positive verifications worth recording

Five load-bearing claims I tried to break and could not.

- **No import cycle.** `PYTHONPATH=$BA_MAIN python3 -c "import bobi.fsutil, bobi.paths, bobi.events.protocol, bobi.events.state, bobi.events.subscriptions, bobi.events.server, bobi.events.adapters, bobi.events, bobi.http"` imports all nine cleanly. `bobi/fsutil.py` and `bobi/events/protocol.py` have zero module-level `bobi` imports, `bobi/events/state.py` imports only `bobi.paths` and `bobi.fsutil`, and `bobi/events/adapters.py` mentions `subscriptions.py` only in a comment. Adding `file_lock`/`atomic_write_text` to `subscriptions.py` and `put_subscriptions` to `server.py` creates no cycle.
- **The pinned subscription tests survive the presence/malformed redesign.** `tests/test_ingress.py:260-276` writes no workspace file, `:306-324` monkeypatches only `explicit_subscriptions`, and `tests/test_symmetric_node.py:90-102` sets up `package/` only. The `bobi_install` fixture creates an EMPTY `<run>/workspace/` (`tests/conftest.py:267,271`) with no `subscriptions.yaml`, so `workspace_subscriptions` returns `None` in all three. Also still green: `tests/test_ingress.py:290-303`, the uncited pin that `explicit_subscriptions` raises `yaml.YAMLError` on a malformed pack.
- **The record IS safe to read from the deaf hook's daemon thread.** `_safe_resubscribe` runs on `event-client-resub` (`bobi/events/client.py:444-446`), and every writer uses `atomic_write_json` -> `atomic_write_text` (`fsutil.py:126-139`), which renames a temp sibling over the target. A reader sees either the old inode or the new one, never a torn document, and `load_deployment_state` already returns `{}` on `JSONDecodeError`/`OSError` (`state.py:49-52`). The spec's fallback is therefore sound in the delete case; the live risk is the `[]` case (G5), not tearing.
- **"Edit the recorded set" really does solve the monitor-drift trap.** `monitors pause` writes `package/monitors.yaml` under `with_mutable_runtime_package` (`cli.py:2992`) and the manager does not re-PUT, while `service.py:631-633` recomposes from the CURRENT registry. Because the CLI edits the record instead, a paused monitor's topic survives an unrelated `add`. Test 5 pins exactly this. This is the design's strongest property.
- **D4's "two different things" analysis holds.** I looked for a third silent-drop mechanism in the delivery-time grant filter (`core.ts:1939-1961`), which can admit nobody for a topic the deployment legitimately holds. `grep -rn "removeResourceGrant\|revokeResourceGrant" event-server/core/src/core.ts` returns nothing and no adapter sets an `expirationTtl`, so a grant can only be missing if it was never written, which the PUT's 400 already names. Spec 125-132 is correct.

## Cruft still present

Against "smallest correct design, no cruft", revision 2 is close to the floor.
Three items.

- **CR1. Spec 134-140, "Two stale facts in the current code".** Two unrelated drive-by fixes (a docstring clause and a log level) carried as a design section with four citations. Both changes are right and both belong in the PR. The section can be two bullets in "Exact changes per file" and nothing else.
- **CR2. Spec 32 and 420 both say "no repo concepts in `bobi/`".** Round 1's C5, half-folded.
- **CR3. The "Out of scope" list's last bullet (spec 426), "A pure grant-check that does not write".** It argues against a thing nobody proposed in revision 2, since step 5 now uses `filter_unauthorized=False` and no filtering is involved. It is a leftover of round 1's F5 debate.

Nothing in "Out of scope" is actually required, with one qualification: the `whatsapp:`/`discord:`/new-workspace-`slack:` limitation from G6 is not listed anywhere and belongs there.

The things that LOOK like cruft and are not: the `put_subscriptions` module-level helper (genuinely unreusable as a closure, confirmed at `subagent.py:1926` closing over `es_url` and `protocol_payload`); the `debug` -> `warning` raise at `client.py:518-519` (the only trace of a voided repair PUT, and G5 makes voiding easier); and Q1/Q2 as open questions rather than decisions (both are real judgement calls with the trade-off stated).

## Missing coverage

Required for correctness, absent from the design, the test plan and "Out of scope".

1. **Seeding an auto-detecting pack (G1).** Neither the design nor test 14 covers a pack with no `subscribe:`, which is the majority case.
2. **The deployment record's concurrency (G2).** Test 13 covers the workspace file only.
3. **The PUT response with no `subscriptions` member (G5).** Four existing tests already produce that body.
4. **`tests/test_event_subscription.py`'s three exact-shape asserts (G7).** Not in the change list.
5. **`bobi doctor` on a malformed workspace file (G8).** Not in the trace, not in test 9.
6. **`whatsapp:`/`discord:`/new-workspace-`slack:` adds (G6).** No test, no stated limitation.
7. **`subscriptions list` with no record (G10).**
8. **A `remove` whose survivors depend on an older boot's grants (G9).**
9. **`tests/test_ingress.py:290-303`.** The pin on `explicit_subscriptions` raising is the test closest to the design change and the spec never cites it; test 10 should extend it with a malformed-WORKSPACE case so the raise is pinned on the new layer too.
10. **An interaction the spec does not mention:** `clear_manager_session` (`bobi/service.py:147-157`) rmtree's `state/deployments/`, which deletes the record the CLI and the deaf hook now depend on, and leaves the `.lock` companion file G2 would introduce. One sentence on what `--fresh`/clear means for the accepted set.

## Is it still a superset of the issue's ask?

Yes, on the evidence available without `gh`.
Judging from the Problem section (spec 14-22) and D1-D5, the spec delivers all five decisions: a workspace-owned list (D2), a generic topic CLI that hot-applies over the existing identity-preserving PUT with no restart (D1, D3), a read-back that reuses the PUT response rather than adding a GET route and is not on the admin channel (D4), and `managed_repos` moving to a director-maintained workspace file as a prompt/pack-only change (D5).
It adds two incidental in-scope fixes (`client.py:501-502`, `:518-519`) and nothing else beyond the ask.
One honest gap against D4's wording: the recorded accepted set is what the server last ACCEPTED, not "what the event server actually routes", since routing additionally applies the delivery-time grant filter. In practice they coincide (no revocation route exists, verified above), but Q1 option (a) should say so in one clause rather than leaving "live" undefined.

## Could not verify

- **Issue #952's text and the Slack thread `1791519568.830069`.** No `gh`, no Slack, per the brief. D1-D5 are taken as quoted and the superset judgement above is derived from the Problem section only.
- **Slack and Linear auto-detection output for THIS deployment.** `_detect_slack` (`bobi/events/adapters.py:176-205`) and `_detect_linear` (`:290-315`) make live API calls. The GitHub leg was executed; the other two are argued from the code and its own comments at `:139-142` and `:309`.
- **Whether a deployed fleet instance's `<run>/workspace/` survives a full re-provision** (as opposed to an in-place install, which reaches `seed_workspace`). `provision-instance.sh` and the volume lifecycle live in the private `bobi-deploy/` tree and were not traced. This matters only to D5's `managed-repos.yaml` seed, not to the subscriptions file, which the manager seeds at boot.
- **Whether the spec's test 2/3/4/5 would actually pass end to end.** I located the harnesses they need (`tests/integration/test_e2e_event_flow.py`, `tests/integration/test_event_isolation.py:101-191`, `tests/integration/test_event_server.py:371`/`:394`/`:62`) and confirmed they boot a real manager and a real event server, but I did not run them: the local server needs `cd event-server && npm ci` plus a selected Node, which the brief's no-network rule forbids.
- **PR #956's superseded design.** Not fetched; reviewed this spec on its own terms.
