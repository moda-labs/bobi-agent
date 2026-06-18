---
description: Run integration tests against the in-repo dogfood-content-review pack. Installs into a throwaway temp project. Tests CLI, event server, workflows, config, and SDK. Writes failing tests before fixing bugs.
---

# Modastack Dogfood Integration Tests

You are running a comprehensive integration test battery against modastack
using the in-repo `agents/dogfood-content-review/` pack installed into a throwaway
temp project. No external repo is needed — the pack lives in this repo.

The test team is `dogfood-content-review` (roles: researcher, editor, fact_checker,
manager). Discover the team name from `agents/registry.yaml` — don't assume it.

## Phase 1: Setup

1. Ensure the dev install is active:
   ```bash
   source ~/dev/modastack/.venv/bin/activate
   pip install -e ~/dev/modastack -q
   ```

2. Create a throwaway temp project and install the pack:
   ```bash
   DOGFOOD_DIR=$(mktemp -d /tmp/modastack-dogfood-XXXXXX)
   cd "$DOGFOOD_DIR"
   git init
   # Copy the pack source so `modastack install` can find it
   cp -r ~/dev/modastack/agents/ "$DOGFOOD_DIR/agents/"
   modastack install agents/dogfood-content-review
   ```
   Save `$DOGFOOD_DIR` — all subsequent commands run from there.

3. Stop any running instances to start clean:
   ```bash
   cd "$DOGFOOD_DIR" && modastack stop 2>/dev/null; modastack event-server stop 2>/dev/null
   ```
   **Orphan check**: the event server's pid file can be lost while the
   process survives (CLI stop will then claim it's not running). Verify
   port 8080 is actually free; if an orphaned `node` process holds it,
   kill it by pid:
   ```bash
   curl -s -m 2 http://localhost:8080/health && lsof -nP -iTCP:8080 -sTCP:LISTEN
   ```
   A long-lived orphan shows a large `deployments` count in /health.

## Phase 2: Feature Scan

Before running tests, scan the modastack source to see if the test battery
is current. Compare the test sections below against the actual codebase.

1. **Scan CLI commands**: Run `modastack --help` and each subgroup's `--help`.
   Note commands added or removed relative to the battery.
2. **Scan event server endpoints**: Read `event-server/src/local.ts` (the
   local server) and `index.ts` (the Cloudflare worker) route tables.
3. **Scan config**: Read `modastack/config.py` — all config is per-project
   (`Config.load(project_path)`); there is no global `~/.modastack/`.
4. **Report findings**: what's new, removed, unchanged. Add inline test
   cases for new features; skip tests for removed ones. If the drift is
   significant, update THIS file as part of the run.

## Phase 3: Test Battery

For each test: print the name, run the action, check the expectation,
print PASS/FAIL. On FAIL continue (don't stop). Summarize at the end.
Run Python checks with `~/dev/modastack/.venv/bin/python`; run CLI
commands from `$DOGFOOD_DIR`.

### Section 1: Project Detection, Config, Team Resolution

```
TEST 1.1: _detect_project_root(Path("$DOGFOOD_DIR")) → contains the temp dir name
TEST 1.2: _detect_project_root(Path("/tmp")) → resolved path ending in "tmp" (never None)
TEST 1.3: Config.load(dogfood_path) → cfg.agent == "<team-name>", cfg.entry_point set
          (Config.load REQUIRES a project path — there is no machine-wide config)
TEST 1.4: cfg.event_services includes the team's event-enabled services (e.g. github, email)
TEST 1.5: cfg.monitors parses the agent.yaml monitors list (commands kept verbatim,
          NOT env-interpolated)
TEST 1.6: cli._resolve_agent_pack("<team-name>", dogfood_path) → <project>/agents/<team-name>
          (resolution: <project>/agents/ first, then <project>/.modastack/agents/)
TEST 1.7: _resolve_agent_pack("nonexistent", dogfood_path) → None
TEST 1.8: cli._list_agent_packs(dogfood_path) → [("<team-name>", "local"), ...]
TEST 1.9: cli._manager_session_name(dogfood_path) == "moda-<entry_point>-<dirname>"
          (the single definition used by start, --fresh, and transcript lookup)
TEST 1.10: config.parse_env_file(dogfood/.modastack/.env) → dict (quotes stripped)
```

### Section 1b: Email Service Subscription (4th-service verification)

```
TEST 1.11: cfg.event_services includes a service named "email" with events=True
TEST 1.12: adapters.is_registered("email") → False (no native adapter)
TEST 1.13: adapters.detect("email", dogfood_path, cfg) → ["email"]
           (fallback — unregistered services return [name])
TEST 1.14: discover_subscriptions(dogfood_path) includes "email" from adapter fallback
TEST 1.15: The CLI monitor-topic logic adds "email/received" to subscriptions
           because email is unregistered and the new-emails monitor declares
           event: email/received. Verify by inspecting _run_from_config behavior:
           the subscribe list should contain both "email" and "email/received".
```

### Section 2: CLI Lifecycle

`modastack start` takes NO team argument — it reads `.modastack/agent.yaml`.

```
TEST 2.1: modastack start → banner with project, pid, agent, workflows, monitors, logs
TEST 2.2: .modastack/state/manager.pid exists, numeric, kill -0 succeeds
TEST 2.3: modastack start again → "Already running"
TEST 2.4: modastack status (after ~10s) → manager shown as running
TEST 2.5: modastack stop → "Stopped." — and if the event server is still up,
          a note: "Event server is still running"
TEST 2.6: pid file removed after stop
TEST 2.7: modastack stop again → "No PID file found" / "not running"
TEST 2.8: start --fresh → "Cleared manager session" AND the saved id file
          .modastack/sessions/moda-<entry_point>-<project>.id is actually emptied
          (regression guard: --fresh used to clear a "moda-mgr-*" name that never existed)
TEST 2.9: start in a directory with no .modastack/agent.yaml → error suggesting
          `modastack install`, non-zero exit
```

### Section 3: Event Server (local Node server on :8080)

```
TEST 3.1: modastack event-server start → "running on port"
TEST 3.2: GET /health → {"status":"ok","mode":"local","deployments":N}
TEST 3.3: modastack event-server status → "running on port 8080" + mode + deployments
TEST 3.4: POST /deployments {"name":...,"subscriptions":["github:test/dogfood"]}
          → 201 with non-empty deployment_id + api_key. SAVE both.
TEST 3.5: /health deployments count incremented
TEST 3.6: POST /webhooks/github (x-github-event: push, payload with repository.full_name
          matching the subscription) → delivered_to >= 1
TEST 3.7: github payload without repository → 400
TEST 3.8: empty body → 400
TEST 3.9: invalid JSON → 400 (local server returns {"error":"invalid JSON"})
TEST 3.10: POST /webhooks/linear → responds with delivered_to (0 is fine)
TEST 3.11: POST /webhooks/slack url_verification → echoes challenge
TEST 3.12: POST /webhooks/slack with x-slack-retry-num header → {"ok":true}, not routed
TEST 3.13: PUT /deployments/$DEP/subscriptions with Bearer $KEY {"add":[...]} →
           subscriptions array includes new entries
TEST 3.13b: same PUT with invalid JSON body → 400
TEST 3.14: same PUT with Bearer bad_key → 403
TEST 3.15: WebSocket ws://localhost:8080/deployments/$DEP/subscribe?last_seen=0
           (Authorization: Bearer $KEY) → "connected" message; then send a webhook
           and receive a live {"type":"event","data":{...,"seq":N}} frame
TEST 3.16: replay — reconnect with last_seen=K where K < current max seq →
           replay frames for seq > K, then "connected".
           NOTE: last_seen=0 intentionally skips replay (fresh start) — do NOT
           expect replays on a first connect.
TEST 3.17: connect with a bad token → socket rejected/closed
TEST 3.18: POST /events/<topic> (generic topic) → responds with delivered_to
TEST 3.19: modastack event-server stop → stopped; port 8080 freed
```

Webhook signatures: the local server only enforces GitHub/Slack signatures
when started with `MODASTACK_ES_WEBHOOK_SECRET` / `MODASTACK_ES_SLACK_SIGNING_SECRET`
in its environment. Default dogfood runs have no secrets, so unsigned
payloads are accepted. (The production Cloudflare worker config differs.)

### Section 3b: Cloudflare Worker parity (wrangler dev)

Section 3 covers the local Node server (`local.ts`). This section runs the SAME
bubble/comms smoke against the **real Cloudflare Worker** (`index.ts` + the
`DeploymentSession` Durable Object + KV) on the `workerd` runtime via
`wrangler dev`. It is the faithful end-to-end Worker check: real runtime, real
HTTP/WS over the wire, exercised by the REAL Python client (`httpx` +
`websocket-client` + `modastack.events.signing`) — so it catches Python↔Worker
HMAC-canonicalization drift and real WS-via-DO issues that the miniflare unit
suite (`event-server/test/index.spec.ts`, in CI per #307/#308) cannot.

`wrangler dev` defaults to **local mode** (no Cloudflare login): real workerd +
simulated KV (local SQLite) + local DO. Skip this section if `wrangler` isn't
installed (`event-server/node_modules/.bin/wrangler`).

Boot: `cd event-server && ./node_modules/.bin/wrangler dev --port 8787` in the
background; wait for `GET http://localhost:8787/health` → `{"status":"ok"}`.
Then, against `http://localhost:8787` (ws `ws://localhost:8787`):

```
TEST 3b.1: POST /deployments (unsigned) → 201 with deployment_id + api_key +
           bubble_id (bub_*) + bubble_key (bkey_*)   [MINT, via KV]
TEST 3b.2: signed POST /events/inbox/<name> → 200 delivered_to>=1
           (sign canonical `timestamp\nnonce\nPOST\n/events/inbox/<name>\nbody`
           with the bubble key; x-moda-bubble/algo/timestamp/nonce/signature)
TEST 3b.3: unsigned POST /events/<topic> → 403   (namespacing is not auth)
TEST 3b.4: wrong-key signed publish → 403
TEST 3b.5: WS /deployments/<id>/subscribe?last_seen=0 (Authorization: Bearer
           <api_key>) → "connected"; then a signed publish → live
           {"type":"event",...} frame   [exercises Worker→DO socket path]
TEST 3b.6: POST /webhooks/github (repo matching subscription) → delivered_to>=1
           (global topic, unsigned ok)
TEST 3b.7: DELETE /deployments/<id> (Bearer <api_key>) → 200   [KV delete, #277]
TEST 3b.8: DELETE with wrong key → 403
```

Teardown: `pkill -f "wrangler dev"; pkill -f workerd`; verify :8787 free.
`event-server/.wrangler/` (local KV/DO state) is gitignored.

Limitations: local-sim KV/DO — does NOT cover real-CF KV eventual consistency,
real DO eviction/hibernation, or the deferred security items S1 (direct-to-DO
auth) / S2 (KV CAS race); see `docs/SECURITY-FINDINGS.md`. For real-deploy
coverage: `wrangler deploy` to staging + run this smoke against the URL.

### Section 4: Workflow System

```
TEST 4.1: modastack workflows list → the team's workflows (from .modastack/workflows/)
TEST 4.2: built-in workflows (e.g. "adhoc") also listed
TEST 4.3: agent prompts see the same menu — prompts.resolver.list_workflows()
          delegates to WorkflowDispatcher (same tiers + dedup as the CLI)
TEST 4.4: workflow.schema.load_workflow(<team workflow yaml>) → name + steps parsed
TEST 4.5: modastack workflows validate <yaml> → exit 0, no errors
TEST 4.6: modastack workflows status → exit 0; shows run_id/name/status/issue,
          step=N and awaiting=<event> for suspended runs (NO node counts —
          the node DAG was removed; the orchestrator is a linear step executor)
```

### Section 5: Role System

Roles are folder-format: `roles/<role>/ROLE.md`, installed into
`.modastack/roles/` by `modastack install`. There is NO framework
built-in roles tier.

```
TEST 5.1: modastack roles list → exit 0, lists the team's roles
TEST 5.2: ls .modastack/roles/ → one folder per role, each with ROLE.md
TEST 5.3: prompts.resolver.build_startup_prompt("<entry_point>", dogfood_path,
          agent_name="<team-name>") → length > 100 and contains the workflow menu
```

### Section 6: SDK and Session Registry

```
TEST 6.1: set_project_root / get_project_root roundtrip
TEST 6.2: SessionRegistry register/get/mark_done roundtrip
          (register an entry, read it back, mark done, status == "done")
TEST 6.3: _sessions_dir() → <dogfood>/.modastack/sessions
          (resolution is cached per project root; set_project_root clears the cache)
TEST 6.4: sdk.state_dir() → <dogfood>/.modastack/state (shared helper —
          events client, monitors, workflow runs, history, kb all use it)
```

### Section 7: Manager Communication (requires running manager — costs a real session)

```
TEST 7.1: modastack start; wait ~30s for the Claude session to initialize
TEST 7.2: modastack message "ping" → "Sent to moda-<entry_point>-<project>"
TEST 7.3: modastack ask "Reply with exactly: DOGFOOD_TEST_OK" --timeout 120
          → output contains DOGFOOD_TEST_OK
          (ask is a hidden alias for message --wait targeting the manager)
TEST 7.4: modastack events → exit 0, recent events listed
TEST 7.5: modastack agents list → "No active agents." (managers are excluded;
          listing is purely registry-backed — there is no in-process agent dict)
TEST 7.6: modastack agents show moda-<entry_point>-<project> →
          Session/Phase/Status lines (find_agent resolves by session name or issue id)
TEST 7.7: modastack transcript show manager -n 10 → recent activity
          (resolves the manager session via _manager_session_name)
```

There is no dashboard — no port file, no /api/status, no /api/event.
Synthetic events go through the event server's generic topic endpoint
(`POST /events/<topic>`), which is what `modastack.events.publish.post_event`
uses (lifecycle emits, monitor verdicts).

### Section 8: Doctor and Version

```
TEST 8.1: modastack --version → version string
TEST 8.2: modastack doctor → checkmark lines; includes Claude CLI/auth,
          project config, install integrity (manifest hash drift),
          services, workflows, event server, recent events
```

There is no `modastack init` command.

### Section 9: Monitor System

```
TEST 9.1: modastack monitors list → exit 0, shows monitors from agent.yaml
          (regression guard: this command once broke on a stale
          MonitorRegistry.load(agent_name=...) call — the loader takes
          only project_path)
TEST 9.2: monitors.schema.parse_interval: "5m"→300, "1h"→3600, "30s"→30
TEST 9.3: parse_interval("bad") raises ValueError
```

### Section 10: Event Pipeline (event server → manager)

```
TEST 10.1: With event server AND manager running (manager auto-starts the
           event server if absent): POST a github issues webhook with a
           repository.full_name matching the temp project's git remote
           (or a synthetic one registered via the subscription) →
           delivered_to >= 1
TEST 10.2: Within ~15s: manager.log contains "Event queued", and
           .modastack/state/events.jsonl gains an entry with the test
           event's type/payload
TEST 10.3: modastack transcript show manager → the event appears in the
           manager's activity
TEST 10.4: Clean up — modastack stop && modastack event-server stop;
           verify pid files gone AND port 8080 actually free (orphan check
           from Phase 1)
```

Use an obviously-synthetic payload (e.g. issue number 999, title
"Dogfood pipeline test — ignore") so the manager doesn't act on it.

### Section 11: Cleanup

```
TEST 11.1: rm -rf "$DOGFOOD_DIR" — remove the throwaway temp project
```

## Phase 4: Results and Coverage Gaps

After all tests complete:

1. **Print summary table**: test name, PASS/FAIL, details for failures.

2. **For each FAIL** — red-green cycle:
   a. Diagnose root cause: test issue or real bug?
   b. Real bug → **write a failing test first** in `~/dev/modastack/tests/`
      that reproduces it. Confirm it fails (red).
   c. Fix the bug in modastack source.
   d. Confirm the new test passes (green), then re-run the dogfood test.
   e. Commit test + fix together.

3. **Print coverage gap report**: modastack areas with no coverage per the
   feature scan.

4. **Update this file** if the battery drifted from the codebase during
   the run.

## Notes

- Always `source ~/dev/modastack/.venv/bin/activate` first.
- The event server runs on port 8080; the local implementation is
  `event-server/dist/local.js` (rebuilt automatically when src is newer).
- WebSocket tests: use the `websocket-client` Python library (installed
  in the venv).
- Manager tests spawn real Claude Code sessions — give them ~30s after
  start before messaging, and always stop them when done.
- Stopping the manager does NOT stop the event server (by design) —
  `modastack stop` prints a reminder when it's still up.
- The temp project is disposable — no standing install state to drift
  between runs.
