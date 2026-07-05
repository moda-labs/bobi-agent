---
description: Run integration tests against the in-repo dogfood-content-review pack using an isolated BOBI_HOME.
---

# Bobi Dogfood Integration Tests

Run a dogfood battery against the in-repo `agents/dogfood-content-review/`
package. The test installs the package into an isolated `BOBI_HOME`, starts a
named Bobi Agent runtime, and exercises the public CLI, event server,
workflows, config, prompts, SDK paths, and optional Fly deploy lifecycle.

Do not use cwd discovery or legacy bare runtime commands. The canonical shape is:

```bash
export BOBI_HOME=<isolated-home>
bobi agents install <source> --name dogfood
bobi agent dogfood status
```

## Phase 1: Setup

1. Ensure the checkout install is active:
   ```bash
   REPO=$(git rev-parse --show-toplevel)
   python3 -m venv "$REPO/.venv"
   source "$REPO/.venv/bin/activate"
   pip install -e "$REPO" -q
   ```

2. Create an isolated Bobi home and install the package:
   ```bash
   export DOGFOOD_AGENT=dogfood
   export BOBI_HOME=$(mktemp -d /tmp/bobi-dogfood-home-XXXXXX)
   export DOGFOOD_EVENT_PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
   export BOBI_EVENT_SERVER="http://localhost:$DOGFOOD_EVENT_PORT"
   export BOBI_ES_TEST_GRANTS_SECRET="bobi-dogfood-test-grants"
   export VENN_API_KEY="${VENN_API_KEY:-dummy}"
   bobi agents install "$REPO/agents/dogfood-content-review" \
     --name "$DOGFOOD_AGENT" --non-interactive
   ```

3. Record these canonical paths:
   ```bash
   export DOGFOOD_RUN="$BOBI_HOME/agents/$DOGFOOD_AGENT/run"
   export DOGFOOD_SRC="$BOBI_HOME/agents/$DOGFOOD_AGENT/src"
   export DOGFOOD_PACKAGE="$DOGFOOD_RUN/package"
   export DOGFOOD_STATE="$DOGFOOD_RUN/state"
   export DOGFOOD_WORKSPACE="$DOGFOOD_RUN/workspace"
   ```

4. Stop any leftover runtime processes for the isolated home:
   ```bash
   bobi agent "$DOGFOOD_AGENT" stop 2>/dev/null || true
   bobi agent "$DOGFOOD_AGENT" event-server stop 2>/dev/null || true
   ```
   Verify the selected test port is free before event-server tests:
   ```bash
   curl -s -m 2 "http://localhost:$DOGFOOD_EVENT_PORT/health" \
     && lsof -nP -iTCP:"$DOGFOOD_EVENT_PORT" -sTCP:LISTEN
   ```

## Deployment Fidelity

The default dogfood battery is a **local named-agent deployment**: it installs a
team into an isolated `BOBI_HOME`, starts the runtime by name, boots a real
manager/Claude session, and drives the same event-server registration and
delivery paths a deployed instance uses.

It is not, by itself, a Fly machine rollout. To emulate production deployment
fully, also run the container/Fly sections:

- Worker parity: run `pytest tests/integration/test_event_server.py` when
  wrangler is installed.
- Container parity: run the Docker/team-image integration tests relevant to the
  change.
- Fly rollout: run Section 10 only with `BOBI_DOGFOOD_FLY=1`; report it as
  **not covered** when skipped.
- Live Slack gateway: run Section 3c only with `BOBI_DOGFOOD_SLACK=1`; report
  it as **not covered** when skipped.

## Phase 2: Feature Scan

Before running tests, scan the current code and update this file if it drifts.

1. Run `bobi --help`, `bobi agents --help`, `bobi agent "$DOGFOOD_AGENT" --help`,
   and key runtime subgroups with `--help`.
2. Read `event-server/src/local.ts` and `event-server/src/index.ts` route tables.
3. Read `bobi/paths.py` and `bobi/config.py`; all runtime paths must derive
   from `BOBI_HOME` + named agent slot.
4. Compare workflow CLI output to `bobi.prompts.resolver.list_workflows(...)`
   rather than a lower-level workflow module.
5. Report new, removed, and unchanged behavior. Add inline tests for new
   features and remove tests for deleted ones.

## Phase 3: Test Battery

For each test, print the name, run the action, check the expectation, print
PASS/FAIL, and continue on failure. Summarize all failures at the end.

### Section 1: Install, Config, Paths

```
TEST 1.1: bobi agents list → includes "$DOGFOOD_AGENT"
TEST 1.2: $DOGFOOD_RUN/package/agent.yaml exists
TEST 1.3: $DOGFOOD_RUN/run does NOT exist; run is the runtime root, not a nested package
TEST 1.4: $DOGFOOD_RUN/state and $DOGFOOD_RUN/workspace exist
TEST 1.5: Config.load(Path("$DOGFOOD_RUN")) → cfg.agent == "dogfood-content-review"
TEST 1.6: paths.agent_name_for_root(Path("$DOGFOOD_RUN")) == "$DOGFOOD_AGENT"
TEST 1.7: paths.resolve_root_for_agent("$DOGFOOD_AGENT") == Path("$DOGFOOD_RUN").resolve()
TEST 1.8: paths.agent_yaml_path(Path("$DOGFOOD_RUN")).resolve()
          == Path("$DOGFOOD_PACKAGE/agent.yaml").resolve()
TEST 1.9: config.parse_env_file("$DOGFOOD_RUN/.env") → dict
TEST 1.10: BOBI_ROOT="$DOGFOOD_RUN" bobi agent "$DOGFOOD_AGENT" status does not depend on cwd
```

### Section 1b: Email Service Subscription

```
TEST 1.11: cfg.event_services includes "email" with events=True
TEST 1.12: adapters.is_registered("email") → False
TEST 1.13: adapters.detect("email", Path("$DOGFOOD_RUN"), cfg) → ["email"]
TEST 1.14: discover_subscriptions(Path("$DOGFOOD_RUN")) includes "email"
TEST 1.15: monitor topic subscriptions include "email/received" from the
           new-emails monitor's event field.
```

### Section 2: Runtime Lifecycle

All runtime commands are scoped by name.

```
TEST 2.1: bobi agent "$DOGFOOD_AGENT" start → banner with bobi version, slot, pid, package, workflows, monitors, logs
TEST 2.2: wait up to 15s for $DOGFOOD_STATE/manager.pid to exist, be numeric, and kill -0 succeeds
TEST 2.3: bobi agent "$DOGFOOD_AGENT" start again → "Already running"
TEST 2.4: bobi agent "$DOGFOOD_AGENT" status after ~10s → manager shown as running
TEST 2.5: bobi agent "$DOGFOOD_AGENT" stop → stopped; pid file removed
TEST 2.6: bobi agent "$DOGFOOD_AGENT" stop again → not running
TEST 2.7: bobi agent "$DOGFOOD_AGENT" start --fresh → manager session id is cleared
TEST 2.8: bare bobi agent <name> start/status/doctor fail because runtime identity is not selected
TEST 2.9: bobi agent missing status → clean error naming the missing package/agent.yaml path
```

### Section 3: Local Event Server

```
TEST 3.1: bobi agent "$DOGFOOD_AGENT" event-server start --port "$DOGFOOD_EVENT_PORT" → running on selected port
TEST 3.2: GET "http://localhost:$DOGFOOD_EVENT_PORT/health" → {"status":"ok","mode":"local","deployments":N}
TEST 3.3: bobi agent "$DOGFOOD_AGENT" event-server status → running on "$DOGFOOD_EVENT_PORT"
TEST 3.4a: POST /deployments {"name":...,"subscriptions":["github:test/dogfood"]}
           without a grant → 400 unauthorized_topics
TEST 3.4b: mint bootstrap deployment, seed a test resource grant for
           github:test/dogfood, then signed POST /deployments with that
           bubble → 201 with deployment_id + api_key
TEST 3.5: /health deployments count incremented
TEST 3.6: POST /webhooks/github with matching repository.full_name → delivered_to >= 1
TEST 3.7: malformed GitHub payloads return 400
TEST 3.8: Slack url_verification echoes challenge
TEST 3.9: Slack retry header returns {"ok":true} and is not routed
TEST 3.10: PUT /deployments/$DEP/subscriptions with Bearer $KEY and {"add":["email"]} adds subscriptions
TEST 3.11: invalid JSON body returns 400
TEST 3.12: bad bearer token returns 403
TEST 3.13: WebSocket subscribe receives connected, live event, and replay frames
TEST 3.14: bad WebSocket token is rejected
TEST 3.15a: unsigned POST /events/<topic> → 403
TEST 3.15b: bubble-signed POST /events/<topic> responds with delivered_to
TEST 3.16: bobi agent "$DOGFOOD_AGENT" event-server stop → stopped; selected port freed
```

### Section 3b: Cloudflare Worker Parity

Skip if `event-server/node_modules/.bin/wrangler` is missing.

```
TEST 3b.1: wrangler dev on a free port boots and /health returns ok
TEST 3b.2: POST /deployments unsigned → 201 with deployment_id, api_key, bubble_id, bubble_key
TEST 3b.3: signed POST /events/inbox/<name> → delivered_to >= 1
TEST 3b.4: unsigned POST /events/<topic> → 403
TEST 3b.5: wrong-key signed publish → 403
TEST 3b.6: WS subscribe + signed publish → live event frame
TEST 3b.7: matching GitHub webhook → delivered_to >= 1
TEST 3b.8: DELETE deployment with api_key → 200; wrong key → 403
```

Teardown: stop wrangler/workerd and verify the selected wrangler port is free.

### Section 3c: Live Slack Gateway

Skip unless `BOBI_DOGFOOD_SLACK=1` and `$REPO/.bobi-dogfood.env` provides
`SLACK_BOT_TOKEN` and `SLACK_TEST_CHANNEL` (a dev-workspace bot with
`chat:write`, `files:write`, `channels:history`, invited to the sacrificial
test channel). When skipped, report live Slack as **not covered**.

This is the recurring soak for the channel gateway (#190/#643): it proves the
real Slack API accepts what the gateway sends (`markdown_text` on postMessage
AND chat.update, placeholder edits, file uploads, over-budget chunking). The
assertions live in pytest so shell runs, dogfood, and future CI share one
code path.

```
TEST 3c.1: set -a; source "$REPO/.bobi-dogfood.env"; set +a
           pytest tests/integration/test_slack_live.py -m live → all pass
TEST 3c.2: open the test channel in Slack and eyeball the thread this run
           created (root message is labeled "Live gateway soak `soak-<ts>`"):
           the edited placeholder renders headers/bold/code as markdown, the
           files are attached, and the over-budget reply arrives as several
           messages with no _(truncated)_ marker and no broken code fences.
           Rendering fidelity is the one thing the pytest file cannot assert.
```

### Section 4: Workflows and Roles

```
TEST 4.1: bobi agent "$DOGFOOD_AGENT" workflows list → dogfood workflows from run/package/workflows
TEST 4.2: built-in workflows such as adhoc also appear
TEST 4.3: bobi.prompts.resolver.list_workflows(Path("$DOGFOOD_RUN")) matches the CLI menu
TEST 4.4: workflow.schema.load_workflow("$DOGFOOD_PACKAGE/workflows/<workflow>.yaml") parses
TEST 4.5: bobi agent "$DOGFOOD_AGENT" workflows validate <yaml> → exit 0
TEST 4.6: bobi agent "$DOGFOOD_AGENT" workflows status → exit 0
TEST 4.7: bobi agent "$DOGFOOD_AGENT" roles list → lists package roles
TEST 4.8: $DOGFOOD_PACKAGE/roles/*/ROLE.md exist
```

### Section 5: SDK and Session Registry

```
TEST 5.1: set_project_root(Path("$DOGFOOD_RUN")) / get_project_root roundtrip
TEST 5.2: SessionRegistry register/get/mark_done roundtrip
TEST 5.3: _sessions_dir() → "$DOGFOOD_STATE/sessions"
TEST 5.4: sdk.state_dir() → "$DOGFOOD_STATE"
```

### Section 6: Manager Communication

This costs a real Claude session.

```
TEST 6.1: bobi agent "$DOGFOOD_AGENT" start; wait for Claude session initialization
TEST 6.2: bobi agent "$DOGFOOD_AGENT" message "ping" → sent to manager
TEST 6.3: bobi agent "$DOGFOOD_AGENT" ask "Reply with exactly: DOGFOOD_TEST_OK" --timeout 120
          → output contains DOGFOOD_TEST_OK
TEST 6.4: bobi agent "$DOGFOOD_AGENT" events → recent events listed
TEST 6.5: bobi agent "$DOGFOOD_AGENT" subagents list → active sub-agents or none
TEST 6.6: bobi agent "$DOGFOOD_AGENT" transcript show manager -n 10 → recent activity
```

### Section 7: Doctor and Version

```
TEST 7.1: bobi --version → version string
TEST 7.2: bobi agent "$DOGFOOD_AGENT" doctor → required checks pass or explain missing credentials
```

### Section 8: Monitor System

```
TEST 8.1: bobi agent "$DOGFOOD_AGENT" monitors list → monitors from agent.yaml/package
TEST 8.2: monitors.schema.parse_interval: "5m"→300, "1h"→3600, "30s"→30
TEST 8.3: parse_interval("bad") raises ValueError
```

### Section 9: Event Pipeline

```
TEST 9.1: With event server and manager running, start with
          --subscribe github:test/dogfood and seed/authorize the test GitHub
          resource, then POST a synthetic GitHub issue event for that repo
          → delivered_to >= 1
TEST 9.2: Within ~15s, manager.log contains event delivery evidence
TEST 9.3: bobi agent "$DOGFOOD_AGENT" transcript show manager → event appears in activity
TEST 9.4: Clean up by stopping the manager and event server; pid files gone and selected port free
```

### Section 10: Fly Deployment Lifecycle

This is the only section that validates real Fly deployment behavior. Skip
unless `fly auth whoami` succeeds and `BOBI_DOGFOOD_FLY=1`; if skipped, report
that Fly rollout was not covered by the dogfood run.

```
TEST 10.1: bobi deploy <throwaway> --team-url <smoke-team-url> --fleet mdftest → boots
TEST 10.2: fly status -a <throwaway> → one started machine
TEST 10.3: instance self-registers on event server
TEST 10.4: re-run deploy → idempotent update, no second machine
TEST 10.5: bobi destroy <throwaway> --yes → app and volume removed
```

Always tear down the Fly app, even after failure.
