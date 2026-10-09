# Review 1: Workspace-owned event subscriptions
Reviewer: independent, fresh context. Pinned at bobi-agent 83bebe49 / moda-agents bf9d1088.

Pinned paths used for every citation:
- `BA_SPEC=/data/.bobi/agents/eng-team/run/worktrees/952-subscriptions` (HEAD `69587c26`)
- `BA_MAIN=/data/.bobi/agents/eng-team/run/worktrees/main-verify-1116` (HEAD `83bebe49af55523dec7c587aedfd7f6cbd205343`)
- `MA_MAIN=/data/.bobi/agents/eng-team/run/worktrees/ma-verify-1116` (HEAD `bf9d1088ad08bafd175232daeddcce3ab2d7e76c`)

All three verified with `git -C <dir> rev-parse HEAD`.
No `gh`, no network writes, no edits outside `/tmp/952rev/`.

## Verdict

Not implementable as written.
The spec's bobi-agent citations are accurate (34 of 37 exact), the `composed_subscriptions` lift out of `bobi/service.py:625-642` is genuinely faithful, and N1/N2/N3/C1/C2/C4 all check out.
But the central design claim is false: `composed_subscriptions` is **not** "the full topic set the deployment should hold".
It omits `inbox/<session>`, which is appended one layer up at `bobi/session.py:1657`, so both new PUT call sites (the CLI and `_resubscribe_on_deaf`) would issue a `replace` that **deletes the manager's own inbox topic** from its live deployment, making the running team unaddressable by `message`, `ask`, and every inter-agent send until the next restart.
The same two lines carry a second blocker: `_resubscribe_on_deaf` is not manager-only, it is the shared deaf hook for **every** session (`bobi/session.py:1673` and `:1710` are the only callers of `_start_event_subscription`), so building its list from `composed_subscriptions(project_path)` would, on any worker's deaf reconnect, replace that worker's inbox-only set with the manager's full `github:`/`slack:`/`linear:` set.
That is a line-for-line reproduction of the 2026-06-12 incident `tests/integration/test_event_isolation.py` exists to prevent.
Third blocker: the spec specifies only absent and empty for `workspace_subscriptions`, and the malformed case falls into a **test-pinned** `except Exception: pass` (`tests/test_ingress.py:306-324`) that routes straight to the auto-detection the whole C1 argument exists to close.
On the moda-agents side every single line range is wrong, and the release-ordering section rests on a stale pin read (`BOBI_VERSION=0.59.0`, not `0.58.0`) plus a workflow citation that does not read the pin at all.

## Citation audit

37 distinct file:line claims. 27 CONFIRMED, 6 WRONG-LINE, 4 MISDESCRIBED, 0 NOT-FOUND.
Commands below were run with `BA_MAIN`/`MA_MAIN` as above.

### bobi-agent

| # | Spec line | Citation | Command | Verdict | Correction |
|---|---|---|---|---|---|
| 1 | 48 | `bobi/events/subscriptions.py:61` gates on `if explicit:` | `sed -n '61p'` -> `        if explicit:` | CONFIRMED | |
| 2 | 49 | `:68-78` calls `Config.load` then `detect()`, returns `[project_path.name]` | `sed -n '68,78p'` -> `from bobi.config import Config` / `cfg = Config.load` (69) / `detect(svc.name, ...)` (73) / `return [project_path.name]` (78) | CONFIRMED | |
| 3 | 137 | `:43-48` requires a dict, reads `raw.get("subscribe", [])` | `sed -n '43,48p'` -> `raw = yaml.safe_load(...)` / `if not isinstance(raw, dict)` (44) / `raw.get("subscribe", [])` (47) | CONFIRMED | |
| 4 | 137 | docstring says "The ONE parser for the `subscribe:` surface (D078)" | `sed -n '28p'` | CONFIRMED | |
| 5 | 233 | `_normalize_explicit_subscriptions` at `:13` | `grep -n '^def _normalize'` -> `13:` | CONFIRMED | |
| 6 | 233 | env interpolation at `:46-48` | `sed -n '46,48p'` -> `_interpolate_env(...)` | CONFIRMED | |
| 7 | 54 | `bobi/events/server.py:849-875` `register()` returns a fresh `(deployment_id, api_key)` | `sed -n '849,875p'` -> `def register(` at 849, `return result["deployment_id"], result["api_key"]` at 875 | CONFIRMED | |
| 8 | 85 | `bobi/events/server.py:682` is `filter_unauthorized: bool = True` | `sed -n '682p'` -> `                        *, filter_unauthorized: bool = True,` | CONFIRMED | |
| 9 | 244 | `authorize_resources` signature `:680-684` | `sed -n '680,684p'` | CONFIRMED | |
| 10 | 58 | `bobi/subagent.py:1765` sets `active_subscriptions` | `grep -n active_subscriptions` -> `1765:    active_subscriptions = list(subscribe)` | CONFIRMED | |
| 11 | 58 | reassigned at `:1835` and `:1892` | same grep -> `1835:`, `1892:` | CONFIRMED | |
| 12 | 250 | full site list `:1765, :1812, :1835, :1867, :1892` | same grep also returns `1982` | WRONG-LINE | the read site `:1982` is missing from the list |
| 13 | 59 | `_resubscribe_on_deaf` at `:1974-1982` | `sed -n '1974,1982p'` -> `def _resubscribe_on_deaf()` at 1974, `_put_subscriptions(...)` at 1982 | CONFIRMED | |
| 14 | 70 | `_register_with_retry` unlinks the cursor at `:1838` | `sed -n '1838p'` -> `cursor_path.unlink(missing_ok=True)` | CONFIRMED | |
| 15 | 71 | `_sync_saved_deployment`'s PUT error path `:1893-1924` | `sed -n '1893,1894p'` -> 1893 is `return dep, key` (success), 1894 is `except EventProtocolError:` | WRONG-LINE | `:1894-1924` |
| 16 | 71 | operator message at `:1917-1920` | `sed -n '1917,1920p'` -> `"...saved deployment and completion cursor retained; retry pending"` | CONFIRMED | |
| 17 | 86, 248 | `filter_unauthorized=False` at `:1883` | `sed -n '1883p'` -> `                filter_unauthorized=False,` | CONFIRMED | |
| 18 | 85 | register path uses it via `:1805-1808` | `sed -n '1805,1809p'` -> `return authorize_resources(` at 1805 | CONFIRMED | |
| 19 | 80, 211, 247 | `_put_subscriptions` at `:1926`, reads/validates/discards at `:1938-1946` | `sed -n '1926,1946p'` -> `def _put_subscriptions(` at 1926, `data = resp.json()` 1939, `validate_server_response(data)` 1946 | CONFIRMED | |
| 20 | 211 | it is a closure over `es_url` inside the session function | `sed -n '1755p'` (`es_url = cfg.event_server_url`), `sed -n '1930p'` | CONFIRMED | also closes over `protocol_payload` |
| 21 | 96-99 | `bobi/service.py:625` read, `:626` extras, `:634-636` monitor keys, `:640-642` lifecycle keys, `:759` reaches the session | `sed -n '600,660p'`, `sed -n '745,760p'` | CONFIRMED | all five exact |
| 22 | 180, 235 | `bobi/service.py:625-642` is the liftable body | `sed -n '625,642p'`; uses only `project_path` and `extra_subscribe` | CONFIRMED | lift is faithful, see F-note under Findings |
| 23 | 205 | `manager_session_name` at `bobi/service.py:135-144` | `sed -n '135,144p'` | CONFIRMED | |
| 24 | 166, 303 | `bobi/supervisor/snapshot.py:107` reads `discover_subscriptions` | `sed -n '107p'` -> `subscriptions = sorted(set(discover_subscriptions(project_root) or []))` | CONFIRMED | |
| 25 | 166, 303 | `bobi/ingress.py:83` | `sed -n '83p'` -> `sources = inbound_event_sources(project_path) + explicit_subscriptions(project_path)` | CONFIRMED | reads `explicit_subscriptions`, **not** `discover_subscriptions`; see F7 |
| 26 | 169, 277 | `seed_workspace` at `bobi/install.py:174-192`, write-if-absent | `sed -n '174,192p'` -> `elif not target.exists(): shutil.copy2(f, target)` at 190-192 | CONFIRMED | |
| 27 | 197 | `monitors` group precedent at `bobi/cli.py:3155` | `sed -n '3155p'` -> `main.add_command(monitors)` | CONFIRMED | |
| 28 | 201 | `_detect_project_root` at `bobi/cli.py:80-95` | `sed -n '80,95p'` | CONFIRMED | |
| 29 | 202 | `atomic_write_text` at `bobi/fsutil.py:100` | `sed -n '100,108p'` -> `def atomic_write_text(` at 100 | CONFIRMED | |
| 30 | 205 | `load_deployment_state` at `bobi/events/state.py:44-52` | `sed -n '44,52p'` | CONFIRMED | |
| 31 | 258 | group list at `bobi/cli.py:4168` | `sed -n '4168p'` -> `for _group_name in ["transcript", "workflows", "roles", "monitors", "kb", "event-server"]:` | CONFIRMED | |
| 32 | 259 | pop list at `:4176-4181` | `sed -n '4176,4181p'` -> loop 4176, list literal 4177-4179, `main.commands.pop(...)` 4181 | CONFIRMED | list literal is `:4177-4179`; the range as given is the whole loop |
| 33 | 74, 254 | `bobi/events/client.py:501-502` stale "(and re-registers on failure)"; `:507-519` protocol-error stop + `log.debug` catch-all | `sed -n '498,519p'` -> 501-502 carry the clause, 518 `except Exception as e:`, 519 `log.debug(...)` | CONFIRMED | |
| 34 | 64 | `tests/test_memory.py:52,55,62,66` arbitrary memory-index data | `sed -n '48,70p'` | CONFIRMED | |
| 35 | 63 | `grep -rn "managed_repos" bobi/` exits 1 | ran in `$BA_MAIN`: `exit=1`, repo-wide hits only `tests/test_memory.py:52,55,62,66` | CONFIRMED | |
| 36 | 79 | `event-server/core/src/core.ts:1536-1540` returns `{subscriptions, added, removed, protocol}` | `sed -n '1536,1540p'` -> 1536 is `await storage.putDeployment(deployment);`, the `return {` is 1537, the body object is 1539 | WRONG-LINE | `:1537-1540` (body at `:1539`) |
| 37 | 84 | `core.ts:1508-1512` calls `unauthorizedGlobalTopics`, 400s the whole update | `sed -n '1508,1512p'` -> 1508 is the "Reject the whole update (no partial write)" comment, call at 1509, 400 at 1511 | CONFIRMED | |
| 38 | 91 | `core.ts:1498-1500` returns `400 replace[] must not be empty` | `sed -n '1498,1500p'` | CONFIRMED | |
| 39 | 206 | "The `protocol` key is required; `checkEventProtocol` rejects a request without it" | `sed -n '36,37p' core.ts` -> `function checkEventProtocol(...)` then `if (!("protocol" in body)) return null;` | **MISDESCRIBED** | the key is OPTIONAL and explicitly backward-compatible; see F11 |

### moda-agents

| # | Spec line | Citation | Command | Verdict | Correction |
|---|---|---|---|---|---|
| 40 | 14, 268 | `agents/moda-eng-team/agent.yaml:125-132` is `subscribe:` | `grep -n '^subscribe:'` -> `127:`; `sed -n '125,132p'` shows two comment lines then `subscribe:` and only 5 of 7 topics | **WRONG-LINE** | `:127-134` |
| 41 | 270 | the `subscribe:` header comment is `:101-124` | `grep -n '^# Managed event topics'` -> `103:`; `sed -n '101,102p'` is `# compose too.` plus a blank, i.e. the tail of the preceding compose comment | **WRONG-LINE** | `:103-126` |
| 42 | 269, 276 | `managed_repos:` is `:149-160` | `grep -n '^managed_repos:'` -> `151:`; the block runs to EOF at 162 (`wc -l` = 162) | **WRONG-LINE** | `:151-162`; the stated range leaves 161-162 orphaned, see F9 |
| 43 | 108, 272 | `scripts/check-deploy-compose.py:85-94` parses `managed_repos`, fails unless moda-skills maps to `github-issues` | `sed -n '85,94p'` | CONFIRMED | exact |
| 44 | 273 | `:76-83` is the `subscribe:` assertion | `sed -n '76,83p'` -> asserts `"github:moda-labs/moda-skills" not in subscriptions` | CONFIRMED | exact |
| 45 | 319 | `moda-agents` installs `bobi==${BOBI_VERSION}` from `.github/fleet-version` at `release-fleet.yml:342` | `sed -n '342p'` -> `run: pip install "bobi==${{ inputs.version }}" ./bobi_deploy`; `grep -c fleet-version .github/workflows/release-fleet.yml` -> **0** | **MISDESCRIBED** | that line installs the version UNDER TEST from a workflow input; the fleet pin is read by `.github/workflows/deploy-agent-teams.yml:91` and `:177` |
| 46 | 319 | `version-gate.yml:100` rewrites the pin | `sed -n '100p'` -> `sed -i -E "s#^BOBI_VERSION=${old}\$#BOBI_VERSION=${NEW}#" "$pin"` | CONFIRMED | exact |
| 47 | 320, 340 | "The pin currently reads `BOBI_VERSION=0.58.0`" | `grep -n '^BOBI_VERSION' .github/fleet-version` -> `31:BOBI_VERSION=0.59.0` | **MISDESCRIBED** | it reads `0.59.0`; see F4 |
| 48 | 170 | the eng-team pack has no `workspace/` directory today | `ls -la agents/moda-eng-team/` -> `agent.md agent.yaml context monitors roles tools workflows`, no `workspace` | CONFIRMED | |
| 49 | 127-133 | the seeded list (spec's example file body) | pack says `github:moda-labs/lightweave`; the spec's example says `github:underminedsk/lightweave` | **MISDESCRIBED** | see F12 |

## Findings

### F1. `composed_subscriptions` omits `inbox/<session>`, so both new PUT sites unsubscribe the manager's own inbox  [severity: blocker]

**Claim:** The spec says (line 174) "A new function returns the full topic set the deployment should hold", lifts it from `bobi/service.py:625-642`, and then uses it to build a `{"replace": [...]}` PUT from the CLI (step 3, line 203) and from `_resubscribe_on_deaf` (line 249).
It is not the full set.

**Evidence:**
```
$ sed -n '625,642p' $BA_MAIN/bobi/service.py     # the liftable body
625:    subscribe = discover_subscriptions(project_path)
626:    subscribe += [s for s in (extra_subscribe or []) if s not in subscribe]
...  monitor_subscription_keys / lifecycle_subscription_keys only
$ sed -n '753,760p' $BA_MAIN/bobi/service.py
753:    run_persistent_agent(... subscribe=subscribe,)
$ sed -n '1657,1660p' $BA_MAIN/bobi/session.py
1657:        keys = [f"inbox/{self.name}"]
1658:        for key in self._subscribe:
1659:            if key not in keys:
1660:                keys.append(key)
$ sed -n '1673,1674p' $BA_MAIN/bobi/session.py
1673:            subscription = _start_event_subscription(
1674:                self.name, keys, bobi_root(), register_attempts=1)
```
The repo's own docs say the same: `docs/EVENT_SERVER.md:396-398` lists what is added on top of discovery as "`--subscribe` extras, every effective monitor's event topic, the sub-agent lifecycle topics, **and the session's own `inbox/<self>`**".
The composition at `:625-642` covers the first three.
`inbox/<self>` is added at `session.py:1657`, outside anything the spec lifts.

**Why it matters:** `core.ts:1517-1536` makes `replace` authoritative: every stored sub not in `desired` is removed from the index.
A CLI `subscriptions add github:foo/bar` would therefore PUT a list with no `inbox/bobi-eng-team-director` and strip it.
The manager immediately stops receiving `bobi agent <name> message`, `ask`, and every inter-agent inbox send, with no error anywhere: step 8 would print a successful diff.
There is no restart in this flow (that is the point of D3), so the team stays unaddressable until someone notices and restarts it.
The same happens on the first deaf reconnect after the change ships, for every session.

**Fix:** Change the signature to carry the session identity, e.g. `composed_subscriptions(project_path, session_name, extra=()) -> list[str]` returning `[f"inbox/{session_name}"] + <current body>`, and have `session.py:1657-1660` call it instead of hand-prepending.
That is the only way "one definition of what this deployment subscribes to" (spec line 186) is actually true.
Add a test asserting the CLI's PUT list contains `inbox/<manager session>`, and that the deployment still answers `bobi agent <name> message` after a `subscriptions add`.

### F2. `_resubscribe_on_deaf` is every session's hook, so sourcing it from `composed_subscriptions` recreates the 2026-06-12 cross-session leak  [severity: blocker]

**Claim:** Spec line 249 changes `_resubscribe_on_deaf` to build its list from `composed_subscriptions`.
The spec treats that hook as the manager's.
It is not.

**Evidence:**
```
$ grep -rn "_start_event_subscription" $BA_MAIN/bobi/ --include=*.py
bobi/subagent.py:1715:def _start_event_subscription(session_name, subscribe, project_path, ...)
bobi/session.py:1667, 1673      # Session._subscribe_to_events  (boot path)
bobi/session.py:1700, 1710      # background retry loop
```
Those two `session.py` sites are the ONLY callers, and they serve every session: the manager gets `keys = ["inbox/<self>"] + composed`, a worker gets `keys = ["inbox/<self>"]` only.
`_resubscribe_on_deaf` is defined once inside `_start_event_subscription` (`:1974`) and wired into every client (`:1990`).
The code already states the consequence of unioning sets onto one deployment, at `bobi/subagent.py:1727-1733` and `bobi/events/state.py:21-25`, and there is a dedicated regression suite:
```
$ sed -n '1,16p' $BA_MAIN/tests/integration/test_event_isolation.py
"""Event delivery isolation between agent sessions sharing one project root.
 Regression test for the prod incident (2026-06-12): the director and two
 project leads all ran from the same project root. ... every agent received the
 union of everyone's subscriptions: the user's Slack DMs to the director were
 delivered to all project leads, and each lead replied on Slack."""
```

**Why it matters:** After the change, any worker whose socket goes deaf (a routine Cloudflare cycle plus a missed keepalive) PUTs the manager's full `github:`/`slack:`/`linear:` set onto its OWN deployment.
That worker then receives every GitHub, Slack and Linear event the team hears, and its auto-dispatch reactor is not even loaded (`has_external` was computed from its inbox-only list at `:1760`), so the events land in a session with no routing policy.
`tests/integration/test_event_isolation.py` will not catch it, because it never forces a deaf reconnect.

**Fix:** Keep the deaf hook per-session.
Replay the list THIS session registered with, re-read from the persisted source rather than the in-process variable: `composed_subscriptions(...)` only when `has_external` is true, and `[f"inbox/{session_name}"]` otherwise.
Simplest correct form, which also fixes C3 without the branch: re-derive the session's own key list from the same single function F1 proposes, passing `session_name`, and gate the external half on `has_external`.
Add a test: worker session, force the deaf path, assert the PUT carries only `inbox/<worker>`.

### F3. Malformed workspace YAML is unspecified and falls into a test-pinned fall-through to auto-detection  [severity: blocker]

**Claim:** The spec specifies `None` for absent and `[]` for empty (lines 151-152) and asserts (line 154) that "an emptied list stops at the workspace layer and never reaches the `Config.load` plus `detect()` fallthrough at `:68-78`".
It never says what a malformed file does, and the surrounding code makes that the dangerous case.

**Evidence:**
```
$ sed -n '59,66p' $BA_MAIN/bobi/events/subscriptions.py
59:    try:
60:        explicit = explicit_subscriptions(project_path)
61:        if explicit:
62:            return explicit
63:    except Exception:
64:        # An unreadable agent.yaml falls through to auto-detection rather than
65:        # failing the whole discovery - the pre-consolidation behavior here.
66:        pass
```
That swallow is deliberate AND test-pinned:
```
$ sed -n '306,324p' $BA_MAIN/tests/test_ingress.py
def test_discovery_keeps_its_own_fall_through_on_a_parser_error(...):
    """...discovery's `except Exception` still sits at the CALL site.
    Deleting that try/except would turn a parse failure into a hard error..."""
    assert subscriptions.discover_subscriptions(...) == [repo_path.name]
```
Blast radius, measured on THIS deployment rather than argued:
```
$ PYTHONPATH=$BA_MAIN python3 -c "import bobi.events.adapters as a; from pathlib import Path;
  print(a.__file__); print(a._detect_github(Path('/data/.bobi/agents/eng-team/run'), None))"
/data/.bobi/.../main-verify-1116/bobi/events/adapters.py
['github:moda-labs/bobi-agent']
$ for d in /data/.bobi/agents/eng-team/run/*/; do [ -e "$d/.git" ] && echo "$d"; done
/data/.bobi/agents/eng-team/run/repo/
$ sed -n '/^services:/,/^requires:/p' /data/.bobi/agents/eng-team/run/package/agent.yaml
services: github (events: true), slack (events: true), linear (events: true)
```
So the fall-through for eng-team yields:
- github: `['github:moda-labs/bobi-agent']` only. `lightweave`, `moda-agents`, `moda-skills` are gone.
- slack: `_detect_slack` (`adapters.py:176-205`) does a LIVE `auth.test` and, if `channels` fails to resolve, `_slack_keys` returns the bare `slack:<team>:app:<id>` prefix (`:168-173`), i.e. a workspace-wide subscription. `adapters.py:139-142` names that exact hazard: "swallowing it here would silently promote a channel-scoped subscription to a workspace-wide one."
- linear: `_detect_linear` (`:290-315`) queries `{ teams { nodes { key } } }` and subscribes to EVERY team, not just MDS and MOD.

Then `_sync_saved_deployment` PUTs that set with `replace`, so the live routing set is rewritten to it.

**Why it matters:** The pack `subscribe:` is behind a read-only guard (`bobi/runtime_guard.py:143-153` protects only `<run>/package/`), which is exactly why C1's truthiness gate was tolerable.
The new file is in `<run>/workspace/`, which is writable, is edited by the CLI by design, and is advertised to the agent brain as its own scratch space (`bobi/prompts/resolver.py:86-97`: "`<workspace>` holds domain files and work products").
On this very deployment that directory already holds 10+ agent-written markdown files.
One bad hand edit, one torn concurrent write (F8), and the team silently loses three repos, gains a workspace-wide Slack scope and every Linear team, and makes two live API calls on the boot path.
The implementer's only two options are both wrong and the spec names neither: inside the `try` gives the above, outside it hard-fails the manager boot and violates the invariant `tests/test_ingress.py:306` pins.

**Fix:** Specify a third state explicitly.
`workspace_subscriptions` must catch its own parse error and raise a distinct, non-swallowed condition, OR the spec must state that a malformed workspace file makes the boot FAIL LOUD with the file path, and must say what `snapshot.py:107` and the CLI do with it.
Add to the test plan: a malformed `workspace/subscriptions.yaml` must not produce an auto-detected set, asserted against the real detector list, not a mock.

### F4. The release-ordering section rests on a stale pin read, and its workflow citation does not read the pin  [severity: blocker]

**Claim:** Spec line 320: "The pin currently reads `BOBI_VERSION=0.58.0`."
Spec line 319 attributes the `bobi==${BOBI_VERSION}` install to `release-fleet.yml:342`.
Spec line 340 lists "The `BOBI_VERSION=0.58.0` pin lagging the 0.59.0 wheel this container runs" as an out-of-scope observation.

**Evidence:**
```
$ grep -n '^BOBI_VERSION' $MA_MAIN/.github/fleet-version
31:BOBI_VERSION=0.59.0
$ sed -n '342p' $MA_MAIN/.github/workflows/release-fleet.yml
        run: pip install "bobi==${{ inputs.version }}" ./bobi_deploy
$ grep -c 'fleet-version' $MA_MAIN/.github/workflows/release-fleet.yml
0
$ grep -n 'fleet-version' $MA_MAIN/.github/workflows/deploy-agent-teams.yml
91:        run: grep -E '^[A-Z_]+=' .github/fleet-version >> "$GITHUB_ENV"
100:            echo "::error::no BOBI_VERSION in .github/fleet-version"; exit 1; }
177:        run: grep -E '^[A-Z_]+=' .github/fleet-version >> "$GITHUB_ENV"
```

**Why it matters:** The whole three-step ordering argument (lines 322-330) concludes "the director prompt would name commands that do not exist yet on a fleet still pinned to 0.58.0".
That premise is false at current main, so the conclusion is unsupported as written and a reader cannot check it.
The out-of-scope bullet at line 340 reports a drift that does not exist.
And `release-fleet.yml:342` is the release CANARY installing a candidate from a workflow input; citing it as the fleet-pin consumer sends the implementer to the wrong file when they go to confirm the ordering.
This is the one section where a Gate-1 approver is asked to accept a sequencing constraint on faith, and all three of its anchors are wrong.

**Fix:** Re-read the pin at approval time and restate it.
Cite `deploy-agent-teams.yml:91` (and `:177`) as the pin consumers and `release-fleet.yml:342` only as the canary's candidate install.
Delete the out-of-scope bullet at line 340 entirely.

### F5. `authorize_resources` is not a filter: it writes grants over the network, and it silently stops filtering without bubble credentials  [severity: major]

**Claim:** CLI step 4 (line 204): "Filter it through `authorize_resources(..., filter_unauthorized=True)` so one ungranted topic cannot hard-reject the whole update (N2)."

**Evidence:**
```
$ sed -n '680,684p' $BA_MAIN/bobi/events/server.py
def authorize_resources(base_url: str, cfg, subscribe: list[str],
                        bubble_id: str, bubble_key: str,
                        *, filter_unauthorized: bool = True, ...)
$ sed -n '713,714p'
    if not (bubble_id and bubble_key):
        return list(subscribe)  # can't sign - leave the set unchanged
$ sed -n '781,784p'
            granted = _authorize_one_resource(
                base_url, service, resource, credential, bubble_id, bubble_key,)
$ sed -n '650,655p'           # _authorize_one_resource
    resp = signed_request(base_url, "POST", "/resources/authorize",
        {"service": service, "resource": resource, "credential": credential},
        bubble_id, bubble_key, timeout=10.0,)
```
So the call needs a `Config` with `.credential()`, a signed bubble identity it must read from `state/bubble.json` (mode `0600`, verified `ls -la`), and it POSTs the real GitHub/Linear credential to the event server once per global topic at a 10s timeout.

**Why it matters:** Three consequences the spec does not state.
First, step 4 is a side-effecting write, not a filter: a `subscriptions remove` would re-POST credentials for every remaining topic.
Second, if `load_bubble_state` returns `{}` (file absent, or after `clear_bubble_state`), line 713-714 returns the list UNFILTERED, so the PUT carries the ungranted topic, `core.ts:1509-1511` 400s the whole update, and the operator gets a failed live apply after the workspace file was already persisted. That is precisely the failure step 4 exists to prevent, reached silently.
Third, with 6 global topics on eng-team the worst case is ~60s of blocking network work on a CLI command that is advertised as a quick mutation, and the same cost lands on the deaf-reconnect repair path F2 covers, on a daemon thread (`bobi/events/client.py:444-446`).

**Fix:** Say in the spec that the CLI must load `load_bubble_state(project_path)` and FAIL with a clear message when it is empty, rather than proceeding into the unfiltered pass-through.
State that step 4 performs grant writes, and say whether `remove` skips it.
If the intent really is a pure filter, add one: a `filter_only` path that checks grants without re-authorizing.

### F6. `filter_unauthorized=True` on the CLI and deaf paths contradicts the deliberate `False` on the same saved deployment  [severity: major]

**Claim:** The spec keeps `_sync_saved_deployment`'s `filter_unauthorized=False` as out of scope (line 336: "That protects an existing deployment's grants") while setting the CLI (line 204) and the deaf path (line 249) to `True` against the SAME deployment id.

**Evidence:**
```
$ sed -n '1859,1865p' $BA_MAIN/bobi/subagent.py
   A topic we cannot authorize is kept anyway (``filter_unauthorized=False``):
   the server may already hold a no-expiry grant from an earlier start, and
   dropping the topic would silently unsubscribe a valid deployment.
$ sed -n '707,711p' $BA_MAIN/bobi/events/server.py
   ... replacing the deployment's subscriptions with a filtered list would
   silently unsubscribe a valid existing deployment.
```

**Why it matters:** Both quoted comments describe exactly what the CLI would now do.
Scenario: `LINEAR_API_KEY` is rotated out of the container env while the server still holds the no-expiry `linear:MOD` grant from an earlier start.
Boot keeps `linear:MOD` (False).
An operator then runs `subscriptions add github:moda-labs/newrepo`; step 4 drops `linear:MOD` because there is no local credential to authorize it, and the `replace` PUT removes it from the live index.
The team goes deaf to Linear as a side effect of adding an unrelated GitHub repo, and nothing in step 8 flags it as a removal the operator did not ask for.

**Fix:** Use `filter_unauthorized=False` on both new paths, matching the saved-deployment semantics, and rely on the server's 400 plus step 8's reporting for the ungranted case.
If `True` is wanted, the spec must argue why the same deployment deserves two different grant policies, and must list every topic the filter would drop in step 8 as a REMOVAL, not a "dropped" note.

### F7. The ingress read site is `explicit_subscriptions`, which the spec never changes, so test 11 cannot pass and an existing invariant breaks silently  [severity: major]

**Claim:** Spec line 303, test 11: "All three read sites agree on one list: `bobi/service.py`, `bobi/supervisor/snapshot.py:107`, and `bobi/ingress.py:83`."
The spec's change list for `bobi/events/subscriptions.py` (lines 232-236) adds three functions and changes the gate at `:61`, which lives inside `discover_subscriptions`.
`explicit_subscriptions` is untouched.

**Evidence:**
```
$ grep -rn "explicit_subscriptions\|discover_subscriptions" $BA_MAIN/bobi/ --include=*.py
bobi/service.py:625           discover_subscriptions
bobi/supervisor/snapshot.py:107  discover_subscriptions
bobi/ingress.py:78,83         explicit_subscriptions      <-- pack only
$ sed -n '184,190p' $BA_MAIN/bobi/service.py     # second ingress consumer
        warning = check_ingress_reachability(project_path, extra_subscriptions=...)
$ sed -n '260,276p' $BA_MAIN/tests/test_ingress.py
def test_ingress_and_discovery_read_subscribe_identically(...):
    """The ingress warning and the session's real subscriptions share a parser.
    Two copies meant a schema change could make the reachability warning name a
    different set of topics than the session actually subscribed to. ...
    this pins them together."""
```

**Why it matters:** After a `subscriptions add slack:T.../C...`, `discover_subscriptions` returns the workspace list and `explicit_subscriptions` returns the pack list.
The ingress reachability warning, and the startup summary that embeds it via `build_startup_info` (`service.py:187-190`), then describe a different topic set than the session subscribes to.
That is the exact D078 regression the existing test exists to block, and the test will NOT fail: it writes no workspace file, so CI stays green while the invariant is gone.
Test 11 as written is unachievable with the change list as written.

**Fix:** Either put the workspace layer inside `explicit_subscriptions` (so the one parser keeps serving both callers, which is the D078 reading), or add `bobi/ingress.py:83` to the "Exact changes per file" list and extend `tests/test_ingress.py:260` with a workspace-file case so the pin actually covers the new precedence.
Name whichever you choose in the spec; today it names neither.

### F8. Read-modify-write on `subscriptions.yaml` with no `fsutil.file_lock`, contradicting the repo standard and the spec's own single-writer claim  [severity: major]

**Claim:** Line 165: "The manager is the only writer."
Line 202 (step 2): "Rewrite `workspace/subscriptions.yaml` atomically with `atomic_write_text`."
Those two statements contradict each other within 40 lines, and the second omits the lock.

**Evidence:**
```
$ sed -n '161,172p' $BA_MAIN/bobi/fsutil.py
def file_lock(path): ...
    Pair it with :func:`atomic_write_text` for read-modify-write state (load,
    mutate, save): the atomic write alone keeps the file parseable, but only the
    lock keeps a concurrent updater's change from being overwritten.
```
`CLAUDE.md` in this repo states the same rule: "Read-modify-write state (a load, a mutate, a save) additionally takes `fsutil.file_lock`".
The writers after the change are: manager boot seeding, `subscriptions add`, `subscriptions remove`, and (in practice) the agent brain writing into `workspace/`.

**Why it matters:** Concrete losses, all silent.
Two `add`s in the same turn (an agent can issue them in parallel): last writer wins, one topic dropped from the file while its PUT already applied, so persisted and live diverge permanently.
`add` racing manager-boot seeding on a cold start: the write-if-absent seed can land after the CLI's write and the add is gone.
And a torn write is the direct input to F3.

**Fix:** State in step 2 that the read-modify-write takes `fsutil.file_lock(path)` around load+mutate+save, and that seeding takes the same lock.
Drop the "manager is the only writer" sentence or restate it accurately as "every writer goes through one locked helper".
Add a test: two concurrent `add` calls, assert both topics survive.

### F9. Every moda-agents line range is wrong, and the `managed_repos` range truncates the block  [severity: major]

**Claim:** `subscribe:` at `:125-132`, its header comment at `:101-124`, `managed_repos:` at `:149-160`.

**Evidence:**
```
$ grep -n '^subscribe:\|^managed_repos:\|^# Managed event topics' $MA_MAIN/agents/moda-eng-team/agent.yaml
103:# Managed event topics. Top-level `subscribe:` is an override, not an append:
127:subscribe:
151:managed_repos:
$ wc -l $MA_MAIN/agents/moda-eng-team/agent.yaml
162
$ sed -n '125,126p'   # what the spec's subscribe range actually starts on
# is replaced by `register` on the next manager-session start, so this team
# must be redeployed for the cut to take effect.
$ sed -n '161,162p'   # what :149-160 leaves behind
  - repo: moda-labs/moda-skills
    tracker: github-issues
$ sed -n '101,102p'
# compose too.
(blank)
```
Correct ranges: `subscribe:` `:127-134`, header comment `:103-126`, `managed_repos:` `:151-162`.

**Why it matters:** The `managed_repos` error is not cosmetic.
Line 269 instructs "Remove `managed_repos:` (`:149-160`)".
Applied literally that deletes a comment, a blank line, the key, and the first binding, and leaves `  - repo: moda-labs/moda-skills` / `    tracker: github-issues` at 161-162 as an orphaned list item with no parent key.
The file then either fails to parse or silently merges into whatever key precedes it, and `scripts/check-deploy-compose.py:67-70` would report "composed agent.yaml is unreadable".
The `subscribe:` error silently drops `linear:MDS` and `linear:MOD` (133-134) from the range the spec presents as the seed, which is also where F12's wrong owner came from.
Line 7 of the spec promises every file:line was read at `83bebe49`; that promise does not hold for the companion repo, and the three errors are consistent with reading a different revision.

**Fix:** Restate all three ranges, and say "remove the `managed_repos:` key and its entire block through EOF" rather than a line range, so the edit cannot half-apply.

### F10. A CLI add or remove silently re-syncs monitor topics, because it recomputes composition from mutable package state  [severity: major]

**Claim:** Step 3 builds the PUT from `composed_subscriptions`, which derives monitor keys from `MonitorRegistry.load(project_path).effective_monitors()`.
The spec presents this as a safety property (N4, test 6).
It is also an undeclared coupling, because the monitor set is mutable at runtime.

**Evidence:**
```
$ sed -n '2980,2996p' $BA_MAIN/bobi/cli.py     # monitors pause
    with with_mutable_runtime_package(project_path):
        paused = MonitorRegistry.pause(name, project_path)
    where = str(paths.package_dir(project_path) / "monitors.yaml")
$ sed -n '631,633p' $BA_MAIN/bobi/service.py
    monitor_events = [m.event for m in
        MonitorRegistry.load(project_path=project_path).effective_monitors()]
```
`monitors add` / `pause` / `remove` write `package/monitors.yaml` at runtime, and the manager does NOT re-PUT on those.

**Why it matters:** The live deployment holds the monitor keys as of the last boot.
`subscriptions add <topic>` recomputes from CURRENT monitors and PUTs a `replace`, so it also adds every monitor topic added since boot and REMOVES every monitor topic paused since boot.
A paused monitor's findings stop being delivered as a side effect of an unrelated subscription change, and step 8 ("Print what changed") would not name it, because it compares the operator's topic against the workspace list, not the composed set against the live set.

**Fix:** Have step 8 diff the full composed+filtered set against the previous accepted set and print EVERY add and remove, not just the named topic.
Note the monitor coupling in the spec so an approver knows a `subscriptions` command can change monitor delivery.
Also contradicts line 157: the pack is not "a frozen read-only image that no operator edits at runtime" for `monitors.yaml` (see F17).

### F11. `protocol` is optional on the wire; the spec states the opposite as a requirement  [severity: minor]

**Claim:** Step 6, line 207: "The `protocol` key is required; `checkEventProtocol` rejects a request without it."

**Evidence:**
```
$ sed -n '36,37p' $BA_MAIN/event-server/core/src/core.ts
function checkEventProtocol(body: Record<string, unknown>): HandlerResult | null {
	if (!("protocol" in body)) return null;
$ sed -n '55,60p' $BA_MAIN/bobi/events/protocol.py
def validate_server_response(payload): ...
    A missing ``protocol`` member is the legacy v1 response shape.
```
A body without `protocol` passes.
The field exists for forward compatibility, not as a requirement.

**Why it matters:** Sending it is still correct, so the implementation would work; the harm is a false fact presented as a wire constraint in a spec an approver is asked to trust.
It also invites a wasted test asserting a rejection that cannot happen.

**Fix:** Reword to "include `protocol: protocol_payload()` to match the existing PUT (`bobi/subagent.py:1931`); the server treats its absence as legacy v1 (`core.ts:37`)."

### F12. The example seed file names a repo the pack does not subscribe to  [severity: minor]

**Claim:** The spec's example `subscriptions.yaml` body (lines 126-133) lists `github:underminedsk/lightweave`.

**Evidence:**
```
$ sed -n '127,134p' $MA_MAIN/agents/moda-eng-team/agent.yaml
subscribe:
  - github:moda-labs/lightweave
  ...
$ sed -n '/^subscribe:/,/^managed_repos:/p' /data/.bobi/agents/eng-team/run/package/agent.yaml
- github:moda-labs/lightweave
```
Both the pack at `bf9d1088` and the installed image on this deployment say `moda-labs/lightweave`.

**Why it matters:** The block is presented as what seeding produces for this team, so it will be copied into the test fixture for tests 8 and 9 and into the docs example.
`underminedsk/lightweave` is a different owner, and a `github:` topic for a repo with no grant is exactly the Q2 case, so the wrong fixture would make a passing test for the wrong reason.

**Fix:** Correct the line, and re-derive the whole example block from `agent.yaml:128-134` so it carries all seven topics.

### F13. "Preserving the header comment" has no mechanism; PyYAML cannot round-trip comments  [severity: minor]

**Claim:** Step 2, line 202: rewrite the file "preserving the header comment".

**Evidence:**
```
$ grep -rn "ruamel" $BA_MAIN/pyproject.toml $BA_MAIN/bobi/
(no output)
$ sed -n '/^dependencies/,/^]/p' $BA_MAIN/pyproject.toml | grep -i yaml
    "pyyaml>=6.0",
```
Only PyYAML is available, and `yaml.safe_dump` discards comments.

**Why it matters:** The honest behavior is "the CLI emits a canonical fixed header plus the list, and any comment the operator or agent added is lost".
Since the file is advertised as operator-editable and lives in the agent's own workspace, losing a hand-written note on every `add` is a real surprise, and an implementer reading line 202 may instead hand-roll line-splicing to preserve it, which is strictly worse.

**Fix:** State the mechanism: "the CLI writes a fixed header constant plus `yaml.safe_dump({'subscribe': ...})`; comments other than that header are not preserved." Or declare the file header-free.

### F14. The CLI's event-server URL source, and its empty-config default, are unspecified  [severity: minor]

**Claim:** Step 5 resolves the deployment id and api key; step 6 PUTs to `/deployments/<id>/subscriptions`.
No step names the base URL.

**Evidence:**
```
$ sed -n '1755p' $BA_MAIN/bobi/subagent.py
    es_url = cfg.event_server_url
$ sed -n '1948,1949p'
    if not es_url:
        es_url = "http://localhost:8080"
```
The existing PUT caller applies a default the spec's `put_subscriptions(base_url, ...)` signature cannot supply.

**Why it matters:** A CLI that passes a bare `Config.load(project_path).event_server_url` PUTs to `/deployments/...` with an empty scheme and host whenever the config has no explicit URL, which is the default local case.
It also skips `ensure_running` (`:1950-1955`), so against a local server that is not up the command fails with a transport error rather than starting it.

**Fix:** Add a step: resolve `es_url` from `cfg.event_server_url` with the `http://localhost:8080` fallback, and `ensure_running` when `local_port_from_url` matches, exactly as `bobi/subagent.py:1948-1955` does.

### F15. Test 7's "must fail before the change" bar is vacuous as written  [severity: minor]

**Claim:** Line 299: "This test must fail against the current `:61` gate; a version that passes before the change is vacuous and does not count."

**Evidence:** Test 7 asserts on `workspace_subscriptions`, which does not exist at `83bebe49` (`grep -n "workspace_subscriptions" $BA_MAIN/bobi/` returns nothing).
Pre-change the test fails with `ImportError`, which proves nothing about the gate.

**Why it matters:** The spec sets a red-test bar and then specifies a test that cannot meet it in substance.
The falsifiable half is "with a workspace file containing `subscribe: []` present, `discover_subscriptions` returns `[]`", and that only becomes red-then-green once the reader is written.

**Fix:** Reframe: "with `workspace/subscriptions.yaml` present and `subscribe: []`, `discover_subscriptions` must return `[]`; written against the pre-change gate (which ignores the file entirely) it returns the pack list or the auto-detected set, and that is the red state."
Also add the malformed case from F3.

### F16. Three off-by-one or incomplete line ranges in bobi-agent  [severity: minor]

**Claim and evidence:**
- Line 71: `_sync_saved_deployment`'s PUT error path at `:1893-1924`. `sed -n '1893,1894p'` shows 1893 is `return dep, key`, the SUCCESS path. Correct: `:1894-1924`.
- Line 79: `core.ts:1536-1540`. `sed -n '1536p'` is `await storage.putDeployment(deployment);`. Correct: `:1537-1540`, body object at `:1539`.
- Line 250: the `active_subscriptions` site list omits `:1982`, the only READ. `grep -n active_subscriptions` returns `1765, 1812, 1835, 1867, 1892, 1982`.

**Why it matters:** Minor individually, but line 7 makes exactness the spec's own contract, and the `:1982` omission is the line the removal depends on ("once nothing reads it").

**Fix:** Correct all three.

### F17. "The pack is a frozen read-only image that no operator edits at runtime" is not true for the package as a whole  [severity: minor]

**Claim:** Line 157, used to justify leaving the pack gate truthiness-based.

**Evidence:** `bobi/runtime_guard.py:143-153` protects `<run>/package/` read-only, but `bobi/cli.py:2992` and the `monitors add` / `remove` commands deliberately lift it with `with_mutable_runtime_package(project_path)` and write `package/monitors.yaml`.

**Why it matters:** The conclusion (leave `:61` alone) is still correct and I am not relitigating it: nothing writes `package/agent.yaml` at runtime, and `tests/test_symmetric_node.py:90-102` pins the current truthiness behavior, which the spec's design preserves.
Only the stated reason is overbroad.

**Fix:** Narrow the sentence to `agent.yaml`: "nothing writes `package/agent.yaml` at runtime (the `monitors` commands write `package/monitors.yaml` under `with_mutable_runtime_package`, `agent.yaml` has no such path)".

### F18. The `docs/` change is a one-line gesture and omits the repo's own CLI reference  [severity: minor]

**Claim:** Line 262: "Document the file and the three commands wherever `subscribe:` is currently documented as pack-only."
No file list.

**Evidence:** The sites, derived mechanically (`grep -rn "subscribe" docs/ skills/ README.md`):
- `docs/EVENT_SERVER.md:378-398`, the "Declaring subscriptions" resolution order. `:382` is "1. **Explicit** - `agent.yaml` top-level `subscribe:`", and `:396-398` enumerates what is added on top. Both become wrong.
- `docs/BUILDING_AGENT_TEAMS.md:129-137`, "explicit subscription override - rarely needed".
- `skills/bobi.md`, which this repo's `CLAUDE.md` names as "the CLI command reference". A new CLI group that is absent from it is a documented-surface gap.

**Why it matters:** `CLAUDE.md` requires affected docs in the same PR, never a follow-up.
An unnamed "wherever" is not a reviewable instruction, and `skills/bobi.md` is the one a reader is most likely to skip.

**Fix:** List the four sites, and say that `EVENT_SERVER.md:378-394` gains a precedence level 0 for the workspace file.

### F19. D5's premise that the director ROLE prompt already reads `managed_repos` is unverified  [severity: minor]

**Claim:** Line 34: "update the eng-team director ROLE prompt to read and keep it current".
`agent.yaml:155` also asserts "Advisory tracker binding read by the director prompt."

**Evidence:**
```
$ grep -rn "managed_repos" $MA_MAIN/agents/
agents/moda-eng-team/agent.yaml:151:managed_repos:
$ grep -n "tracker" $MA_MAIN/agents/moda-eng-team/roles/director/ROLE.md
11:like `WEB`, `API`, `JOB`). Where the core director reads tracker bindings
12:from configuration and the `## Team Policy` block, read Moda's tracker as
22:- A managed repo may explicitly set `tracker: github-issues` in configuration.
```
The prompt says "from configuration" and "in configuration"; it never names `managed_repos`.

**Why it matters:** The change is an ADD to the prompt, not an update, and the two lines that must change (`ROLE.md:11-12` and `:22`) are the only moda-agents edits in the spec with no citation at all.
It also means `managed_repos` has exactly ONE real consumer today, `scripts/check-deploy-compose.py:85-94`, which strengthens D5 rather than weakening it.

**Fix:** Cite `ROLE.md:11-12` and `:22` as the lines that must point at `workspace/managed-repos.yaml`, and drop the now-false comment at `agent.yaml:155` along with the key.

### F20. The baohua pack documents the old precedence and is not in the change list  [severity: nit]

**Claim:** The companion PR section covers only `moda-eng-team` and `scripts/`.

**Evidence:**
```
$ grep -rn "subscribe" $MA_MAIN/agents/baohua/
agents/baohua/README.md:152:- **`subscribe:` must list Slack explicitly.** An explicit list short-circuits
agents/baohua/agent.md:11:... The `subscribe:` list is pinned rather than auto-detected, so it never
agents/baohua/agent.yaml:105:# `subscribe:` list is used verbatim and short-circuits the github service's
agents/baohua/agent.yaml:118:# RETURNS on the first branch when `subscribe:` is non-empty, so step 2 - services
agents/baohua/agent.yaml:160:subscribe:
```
`agent.yaml:118` describes `subscriptions.py:61-62` by behavior, and that description stops being true once a workspace layer precedes it.

**Why it matters:** Cosmetic only; baohua keeps working (the seed is still its pack list).

**Fix:** One line in the companion PR section noting `agents/baohua/agent.yaml:105,118` and `README.md:152` describe the old precedence.

### Positive verifications worth recording

Four load-bearing claims I tried to break and could not.
- The `composed_subscriptions` lift is faithful for everything except `inbox/` (F1). `sed -n '625,642p' bobi/service.py` reads only `project_path` and `extra_subscribe`; no `cfg`, no `session_name`, no `deployment`, no env. `MonitorRegistry.load(project_path=...)` takes the path explicitly (`bobi/monitors/registry.py:79`) and `bobi/monitors/registry.py` imports only `bobi.paths` and `.schema`, so the lift into `bobi/events/subscriptions.py` creates no import cycle with `bobi/events/__init__.py:15`.
- No `workspace/` collision. `grep -rn "workspace_dir" bobi/` gives `cli.py:838` (mkdir only), `webapp/server.py:294` (mkdir), `doctor.py:329` (existence check), `install.py:184` (write-if-absent). `write_install_manifest` (`install.py:195-220`) hashes only `roles tools workflows monitors context` plus three top-level files, so a director edit raises no doctor drift. `compose.py:348-366` `merge_workspace` is deploy-flatten only and runs on the host; the instance path is `bobi agents install` (`bobi-deploy/.../deploy.py:2047-2051`), which reaches `seed_workspace`. The spec's D5 seeding claim holds.
- `<run>/workspace/` is writable. `runtime_guard.protected_runtime_roots` (`:143-153`) protects only `<run>/package/`.
- The real local event server fixture exists and the integration tests 1-6 are runnable in it: `tests/integration/test_event_server.py:370` `event_server` (module-scoped, parametrized local + wrangler), `:394` `deployment`, `:1271` `_put_subscriptions`, `:62` `_seed_resource_grants`.

## Cruft to cut

Against the "smallest correct design, no cruft" bar.

**C1. The cached accepted set in `state/`.** Cut step 7 (line 208), the whole "Why the accepted set is cached in `state/` and not `workspace/`" section (lines 223-227), the age-labelled display in `list` (lines 218-221), and Q1 (lines 344-348).
That is a new state file, a new write path, a timestamp, a display rule, a spec subsection and an open question, in service of showing data the spec itself says is stale by construction and that nothing refreshes.
Q1 also misses a third option: an idempotent `{"replace": <current composed set>}` returns the live accepted set with `added: 0, removed: 0` and adds no server surface. The existing test at `tests/integration/test_event_server.py:510-520` proves that shape. It does write (`core.ts:1531-1536` always calls `addSubscription` and `putDeployment`), so "a read command must not mutate" is a fair objection, but it is the honest third option and the spec should weigh it rather than default to a cache that satisfies D4's letter and not its intent.
Minimal replacement: `list` prints the persisted workspace list and the composed set it WOULD PUT, and says explicitly that live confirmation requires a mutation or a GET. One paragraph, zero new files.

**C2. Integration tests 1 and 3 duplicate existing coverage.**
Test 1 (`replace` returns the accepted set) is covered twice: `event-server/test/core.spec.ts:2284-2306` asserts `body.subscriptions`, `added: 1`, `removed: 1`; `tests/integration/test_event_server.py:510-520` asserts it over real HTTP.
Test 3 (`{"replace": []}` -> 400) is covered at `event-server/test/core.spec.ts:2314-2324`.
Cut both and add the one missing assertion to the existing integration test.
Test 2 is genuinely new and should stay: `grep -n "unauthorizedGlobalTopics\|unauthorized_topics" event-server/test/core.spec.ts` shows the PUT path is NOT covered, only `handleRegisterDeployment` (`:2008-2035`).

**C3. The out-of-scope `BOBI_VERSION` bullet (line 340).** Factually wrong (F4); delete.

**C4. S1's seven lines (68-74).** It concludes "this August hazard is gone". Two lines would do: `_sync_saved_deployment`'s error path retains both (`:1894-1924`, message at `:1917-1920`); only `_register_with_retry` unlinks (`:1838`).
Keep the `client.py:501-502` docstring correction, it is one line and the statement is false.

**C5. Prose repetition.** "no repo concepts in `bobi/`" appears at lines 27 and 334. The N4 trap is stated at 100, 185 and 294. State each once.

**Keep, despite looking like cruft:**
- `put_subscriptions` as a module-level helper. Two callers today plus the CLI, and the existing one is a closure (`:1926`), so it genuinely cannot be reused. `bobi/events/server.py` already imports `httpx` and `atomic_write_text`, and `bobi/events/protocol.py` imports nothing from `server`, so there is no cycle.
- The `debug` -> `warning` raise at `client.py:518-519`. It is the only visibility for a voided repair PUT, and after F6 that PUT becomes easier to void, not harder.

## Missing coverage

Required for correctness but absent or declared out of scope.

- **The malformed-file policy (F3).** Not in the design, not in "Out of scope", not in Open questions. The spec cannot be approved without it.
- **`inbox/<session>` in the composition (F1).** Nothing in the spec or the test plan would catch its loss. Add: after a CLI `add`, `bobi agent <name> message` still reaches the manager.
- **Per-session scoping of the deaf hook (F2).** Add a worker-session deaf-path test.
- **Concurrency (F8).** No test for two concurrent mutations, or a mutation racing boot seeding.
- **`bobi/ingress.py` (F7).** Named in test 11 but absent from the change list.
- **The full add/remove diff (F10).** Step 8 reports the named topic; nothing reports collateral removals from the monitor set or the grant filter.
- **`skills/bobi.md` (F18).** This repo's CLI reference, unnamed.
- **ROLE.md line citations (F19).** The only moda-agents edit with no anchor.
- **What `subscriptions list` does when the manager is down.** Step 12 covers `add`; `list` has no stated behavior when `load_deployment_state` returns `{}`.
- **Who runs the CLI.** D1 is "a running team may mutate its own subscriptions", so the director agent runs it from inside the runtime. The spec never says so, never confirms `_bind_agent_runtime` resolves the installed slot (`eng-team`) rather than the pack `agent:` (`moda-eng-team`) from that context, and only mentions the name trap for the ROLE prompt (line 281). It is the same trap for the command the prompt would print.

## Could not verify

- **Slack and Linear auto-detection output for this deployment.** `_detect_slack` (`adapters.py:176-205`) and `_detect_linear` (`:290-315`) make live API calls. Brief forbids network, so the F3 blast radius for those two services is argued from the code and its own comments (`adapters.py:139-142`, `:168-173`, `:309`), not executed. The github leg WAS executed and returned `['github:moda-labs/bobi-agent']`.
- **Whether a deployed fleet instance's `workspace/` survives a full re-provision** (as opposed to the in-place `update_team_url` path I did verify at `bobi-deploy/.../deploy.py:2047-2051`). `provision-instance.sh` and the volume lifecycle were not traced.
- **Issue #952 and the Slack thread `1791519568.830069`.** Not readable without `gh` or a Slack call, so D1-D5 are taken as quoted.
- **Whether `tests/test_service.py:21-26`'s monkeypatch shim survives the `:625-642` replacement.** It patches `subscriptions.discover_subscriptions`, `monitor_subscription_keys`, `lifecycle_subscription_keys` and `MonitorRegistry.load` as module attributes. If `composed_subscriptions` calls them through module globals the shim still intercepts; if the implementer binds them locally it breaks. I did not run the suite to confirm either way. Worth one sentence in the spec: the lift must keep those as module-global lookups.
- **PR #956's superseded design.** Not fetched; I reviewed this spec on its own terms as instructed.
