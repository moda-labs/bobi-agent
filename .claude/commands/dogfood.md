---
description: Run integration tests against modastack-dogfood. Tests CLI, event server, workflows, config, and SDK. Writes failing tests before fixing bugs.
---

# Modastack Dogfood Integration Tests

You are running a comprehensive integration test battery against modastack
using the `modastack-dogfood` repo as a test harness. This is a real repo
at `~/dev/modastack-dogfood` with custom (non-engineering) roles and workflows.

## Phase 1: Setup

1. Check if `~/dev/modastack-dogfood` exists. If not, clone it:
   ```bash
   gh repo clone moda-labs/modastack-dogfood ~/dev/modastack-dogfood
   ```

2. Check if `.modastack/config.yaml` exists in the dogfood repo. If not,
   report that the repo is misconfigured and stop.

3. Ensure modastack is installed (dev or uv tool):
   ```bash
   source ~/dev/modastack/.venv/bin/activate
   pip install -e ~/dev/modastack -q
   ```

4. Kill any running modastack instances in the dogfood repo to start clean:
   ```bash
   cd ~/dev/modastack-dogfood && modastack stop 2>/dev/null
   ```

## Phase 2: Feature Scan

Before running tests, scan the modastack source to see if the test battery
is current. Compare the test sections below against the actual codebase.

1. **Scan CLI commands**: Run `modastack --help` and each subgroup's `--help`
   to get the current command list. Compare against the test battery below.
   Note any commands that were added or removed.

2. **Scan event server endpoints**: Read `modastack/manager/events/event_server.py`
   and list all routes. Compare against test battery.

3. **Scan config classes**: Read `modastack/config.py` and check field lists
   against what the tests validate.

4. **Report findings**: Print a summary of what's new, what's removed, and
   what's unchanged. If there are new features, add test cases for them
   inline before proceeding. If features were removed, skip those tests.

## Phase 3: Test Battery

Run each test section in order. For each test:
- Print the test name and what it's testing
- Run the action
- Check the expected result
- Print PASS or FAIL with details
- On FAIL, continue to the next test (don't stop)

Track results in a summary table at the end.

### Section 1: Repo Detection and Config Loading

```
TEST 1.1: Repo detection from cwd
  ACTION: cd ~/dev/modastack-dogfood && python3 -c "
    import sys; sys.path.insert(0, '$HOME/dev/modastack')
    from modastack.cli import _detect_repo_root
    from pathlib import Path
    result = _detect_repo_root(Path('$HOME/dev/modastack-dogfood'))
    print(result)
  "
  EXPECT: Output contains "modastack-dogfood"

TEST 1.2: Repo detection fails outside repo
  ACTION: cd /tmp && python3 -c "
    import sys; sys.path.insert(0, '$HOME/dev/modastack')
    from modastack.cli import _detect_repo_root
    from pathlib import Path
    result = _detect_repo_root(Path('/tmp'))
    print(result)
  "
  EXPECT: Output is "None"

TEST 1.3: RepoConfig loads from dogfood
  ACTION: python3 -c "
    import sys; sys.path.insert(0, '$HOME/dev/modastack')
    from pathlib import Path
    from modastack.config import RepoConfig
    rc = RepoConfig.from_file(Path('$HOME/dev/modastack-dogfood'))
    print(f'tracking={rc.task_tracking} project={rc.project} labels={rc.trigger_labels}')
  "
  EXPECT: Output contains "github-issues" and "PLAYBOOK"

TEST 1.4: LocalConfig loads nested YAML correctly
  ACTION: python3 -c "
    import sys; sys.path.insert(0, '$HOME/dev/modastack')
    from pathlib import Path
    from modastack.config import LocalConfig
    local = LocalConfig.load(Path('$HOME/dev/modastack-dogfood'))
    print(f'url={local.event_server_url} port={local.dashboard_port}')
  "
  EXPECT: url is not empty, dashboard_port is an integer > 0

TEST 1.5: LocalConfig returns defaults when local.yaml missing
  ACTION: python3 -c "
    import sys; sys.path.insert(0, '$HOME/dev/modastack')
    from pathlib import Path
    from modastack.config import LocalConfig
    local = LocalConfig.load(Path('/tmp'))
    print(f'url={local.event_server_url!r} port={local.dashboard_port}')
  "
  EXPECT: url is empty string, port is 8095
```

### Section 2: CLI Lifecycle (start/stop/restart)

```
TEST 2.1: Start modastack
  ACTION: cd ~/dev/modastack-dogfood && modastack start
  EXPECT: Output contains "started for modastack-dogfood" and a PID

TEST 2.2: PID file exists after start
  ACTION: cat ~/dev/modastack-dogfood/.modastack/state/manager.pid
  EXPECT: Output is a numeric PID, process is alive (kill -0 PID succeeds)

TEST 2.3: Double-start is rejected
  ACTION: cd ~/dev/modastack-dogfood && modastack start
  EXPECT: Output contains "already running"

TEST 2.4: Status shows running
  ACTION: cd ~/dev/modastack-dogfood && modastack status
  WAIT: 5 seconds for manager to initialize
  EXPECT: Output contains "Manager: running"

TEST 2.5: Stop modastack
  ACTION: cd ~/dev/modastack-dogfood && modastack stop
  EXPECT: Output contains "Stopped"

TEST 2.6: PID file removed after stop
  ACTION: test -f ~/dev/modastack-dogfood/.modastack/state/manager.pid
  EXPECT: File does not exist (exit code 1)

TEST 2.7: Stop when already stopped
  ACTION: cd ~/dev/modastack-dogfood && modastack stop
  EXPECT: Output contains "No PID file found" or "not running"

TEST 2.8: Restart from stopped
  ACTION: cd ~/dev/modastack-dogfood && modastack restart
  EXPECT: Output contains "started for modastack-dogfood"

TEST 2.9: Restart while running
  ACTION: cd ~/dev/modastack-dogfood && modastack restart
  WAIT: 3 seconds
  EXPECT: Output contains "Stopped" or "Stopping" AND "started"

TEST 2.10: Start outside modastack repo
  ACTION: cd /tmp && modastack start 2>&1
  EXPECT: Exit code non-zero, output contains "Not inside a modastack repo"

TEST 2.11: Clean up — stop for next sections
  ACTION: cd ~/dev/modastack-dogfood && modastack stop 2>/dev/null; true
  EXPECT: Always passes (cleanup)
```

### Section 3: Event Server

```
TEST 3.1: Start event server
  ACTION: cd ~/dev/modastack-dogfood && modastack event-server start
  EXPECT: Output contains "running on port"

TEST 3.2: Health endpoint
  ACTION: curl -s http://localhost:8080/health
  EXPECT: JSON with status=ok, mode=local

TEST 3.3: Event server status CLI
  ACTION: cd ~/dev/modastack-dogfood && modastack event-server status
  EXPECT: Output contains "running"

TEST 3.4: Register deployment
  ACTION: curl -s -X POST http://localhost:8080/deployments \
    -H "Content-Type: application/json" \
    -d '{"name":"dogfood-test","subscriptions":["moda-labs/modastack-dogfood"]}'
  EXPECT: JSON with deployment_id and api_key (both non-empty)
  SAVE: deployment_id and api_key for later tests

TEST 3.5: Health shows deployment count
  ACTION: curl -s http://localhost:8080/health
  EXPECT: deployments >= 1

TEST 3.6: GitHub webhook delivery
  ACTION: curl -s -X POST http://localhost:8080/webhooks/github \
    -H "Content-Type: application/json" \
    -H "x-github-event: push" \
    -H "x-github-delivery: test-001" \
    -d '{"ref":"refs/heads/main","repository":{"full_name":"moda-labs/modastack-dogfood"},"pusher":{"name":"test"},"commits":[{"message":"test"}]}'
  EXPECT: JSON with delivered_to >= 1

TEST 3.7: GitHub webhook — missing repo returns 400
  ACTION: curl -s -w "\n%{http_code}" -X POST http://localhost:8080/webhooks/github \
    -H "Content-Type: application/json" -H "x-github-event: push" \
    -d '{"ref":"refs/heads/main"}'
  EXPECT: HTTP 400

TEST 3.8: GitHub webhook — empty body returns 400
  ACTION: curl -s -w "\n%{http_code}" -X POST http://localhost:8080/webhooks/github
  EXPECT: HTTP 400

TEST 3.9: GitHub webhook — invalid JSON returns 400
  ACTION: curl -s -w "\n%{http_code}" -X POST http://localhost:8080/webhooks/github \
    -H "Content-Type: application/json" -d "not json"
  EXPECT: HTTP 400

TEST 3.10: Linear webhook delivery
  ACTION: curl -s -X POST http://localhost:8080/webhooks/linear \
    -H "Content-Type: application/json" \
    -d '{"action":"create","type":"Issue","data":{"id":"1","title":"test","team":{"key":"TEST"}}}'
  EXPECT: JSON with delivered_to (may be 0 if no linear subscription)

TEST 3.11: Slack URL verification
  ACTION: curl -s -X POST http://localhost:8080/webhooks/slack \
    -H "Content-Type: application/json" \
    -d '{"type":"url_verification","challenge":"test_challenge"}'
  EXPECT: JSON with challenge="test_challenge"

TEST 3.12: Slack retry rejection
  ACTION: curl -s -X POST http://localhost:8080/webhooks/slack \
    -H "Content-Type: application/json" \
    -H "x-slack-retry-num: 1" \
    -d '{"type":"event_callback"}'
  EXPECT: JSON with ok=true

TEST 3.13: Update deployment subscriptions
  ACTION: curl -s -X PUT http://localhost:8080/deployments/$DEPLOYMENT_ID/subscriptions \
    -H "Authorization: Bearer $API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"add":["linear:TEST","slack:T12345"]}'
  EXPECT: JSON with subscriptions array containing the new entries

TEST 3.14: Auth — bad API key returns 403
  ACTION: curl -s -w "\n%{http_code}" \
    -X PUT http://localhost:8080/deployments/$DEPLOYMENT_ID/subscriptions \
    -H "Authorization: Bearer bad_key" \
    -H "Content-Type: application/json" -d '{"add":["x"]}'
  EXPECT: HTTP 403

TEST 3.15: WebSocket — connect and receive live event
  ACTION: python3 script that connects WebSocket, sends a webhook, reads event
  EXPECT: Receives event with type=event, source=github

TEST 3.16: WebSocket — replay with last_seen
  ACTION: python3 script that connects with last_seen=0, reads replayed events
  EXPECT: Receives replay messages with seq > 0, then connected message

TEST 3.17: WebSocket — bad token rejected
  ACTION: python3 script that connects with invalid token
  EXPECT: WebSocket closed with code 4003

TEST 3.18: Stop event server
  ACTION: cd ~/dev/modastack-dogfood && modastack event-server stop
  EXPECT: Output contains "stopped"

TEST 3.19: Restart event server
  ACTION: cd ~/dev/modastack-dogfood && modastack event-server restart
  EXPECT: Event server running, health returns ok
```

### Section 4: Workflow System

```
TEST 4.1: Workflow list shows repo-specific workflows
  ACTION: cd ~/dev/modastack-dogfood && modastack workflow list 2>&1
  EXPECT: Output contains "content-lifecycle" AND "research-task" AND "content-review"

TEST 4.2: Built-in workflows also loaded
  ACTION: cd ~/dev/modastack-dogfood && modastack workflow list 2>&1
  EXPECT: Output contains "adhoc" AND "issue-lifecycle"

TEST 4.3: Repo workflows override built-in by name
  ACTION: If dogfood has a workflow with same name as built-in, repo version wins
  EXPECT: Verify via WorkflowDispatcher.format_workflow_menu() dedup logic

TEST 4.4: Workflow YAML parsing
  ACTION: python3 -c "
    import sys; sys.path.insert(0, '$HOME/dev/modastack')
    from modastack.workflow.schema import load_workflow
    from pathlib import Path
    wf = load_workflow(Path('$HOME/dev/modastack-dogfood/.modastack/workflows/content-lifecycle.yaml'))
    print(f'name={wf.name} steps={len(wf.steps)} trigger={wf.trigger[:40]}')
  "
  EXPECT: name=content-lifecycle, steps > 0

TEST 4.5: Workflow validate command
  ACTION: cd ~/dev/modastack-dogfood && modastack workflow validate \
    .modastack/workflows/content-lifecycle.yaml 2>&1
  EXPECT: No errors, shows step names

TEST 4.6: Workflow state directory exists
  ACTION: ls ~/dev/modastack-dogfood/.modastack/state/workflow/ 2>/dev/null
  EXPECT: Directory exists (or is created on first run)
```

### Section 5: Role System

```
TEST 5.1: Role list shows custom roles
  ACTION: cd ~/dev/modastack-dogfood && modastack role list 2>&1
  EXPECT: Output contains "researcher" AND "editor" AND "fact-checker"

TEST 5.2: Built-in engineer role also available
  ACTION: cd ~/dev/modastack-dogfood && modastack role list 2>&1
  EXPECT: Output contains "engineer"

TEST 5.3: Role files exist and are readable
  ACTION: ls ~/dev/modastack-dogfood/.modastack/agents/
  EXPECT: Contains researcher.md, editor.md, fact-checker.md
```

### Section 6: SDK and Session Registry

```
TEST 6.1: set_repo_root / get_repo_root roundtrip
  ACTION: python3 -c "
    import sys; sys.path.insert(0, '$HOME/dev/modastack')
    from pathlib import Path
    from modastack.sdk import set_repo_root, get_repo_root
    set_repo_root(Path('$HOME/dev/modastack-dogfood'))
    print(get_repo_root())
  "
  EXPECT: Output ends with "modastack-dogfood"

TEST 6.2: SessionRegistry register and get
  ACTION: python3 -c "
    import sys; sys.path.insert(0, '$HOME/dev/modastack')
    from pathlib import Path
    from modastack.sdk import set_repo_root, SessionEntry, SessionRegistry
    set_repo_root(Path('$HOME/dev/modastack-dogfood'))
    reg = SessionRegistry()
    reg.register(SessionEntry(name='test-session', role='editor', status='running'))
    got = reg.get('test-session')
    print(f'name={got.name} role={got.role} status={got.status}')
    reg.mark_done('test-session')
    print(f'after_done={reg.get(\"test-session\").status}')
  "
  EXPECT: name=test-session role=editor status=running, after_done=done

TEST 6.3: Sessions dir is per-repo
  ACTION: python3 -c "
    import sys; sys.path.insert(0, '$HOME/dev/modastack')
    from pathlib import Path
    from modastack.sdk import set_repo_root, _sessions_dir
    set_repo_root(Path('$HOME/dev/modastack-dogfood'))
    print(_sessions_dir())
  "
  EXPECT: Path contains "modastack-dogfood/.modastack/sessions"
```

### Section 7: Manager Communication (requires running manager)

```
TEST 7.1: Start manager for communication tests
  ACTION: cd ~/dev/modastack-dogfood && modastack start
  WAIT: 8 seconds for initialization
  EXPECT: Manager started

TEST 7.2: Message command
  ACTION: cd ~/dev/modastack-dogfood && modastack message "dogfood integration test ping"
  EXPECT: Output contains "Sent"

TEST 7.3: Ask command
  ACTION: cd ~/dev/modastack-dogfood && modastack ask \
    "Reply with exactly: DOGFOOD_TEST_OK" --timeout 60
  EXPECT: Output contains "DOGFOOD_TEST_OK"

TEST 7.4: Events command
  ACTION: cd ~/dev/modastack-dogfood && modastack events
  EXPECT: Output shows recent events and decisions

TEST 7.5: Agents list command (no active agents)
  ACTION: cd ~/dev/modastack-dogfood && modastack agents list
  EXPECT: Output contains "No active agents" or empty list

TEST 7.6: Transcript command
  ACTION: cd ~/dev/modastack-dogfood && modastack transcript show manager -n 5
  EXPECT: Output shows manager activity (timestamps, messages)

TEST 7.8: Dashboard port file
  ACTION: cat ~/dev/modastack-dogfood/.modastack/state/dashboard.port
  EXPECT: Numeric port value

TEST 7.9: Dashboard health
  ACTION: Read dashboard port, then curl http://localhost:$PORT/api/status
  EXPECT: JSON with manager key

TEST 7.10: Dashboard event injection
  ACTION: curl -s -X POST http://localhost:$PORT/api/event \
    -H "Content-Type: application/json" \
    -d '{"type":"test.ping","source":"dogfood","data":{"msg":"hello"}}'
  EXPECT: JSON with ok=true

TEST 7.11: Clean up — stop manager
  ACTION: cd ~/dev/modastack-dogfood && modastack stop
  EXPECT: Stopped
```

### Section 8: Doctor and Version

```
TEST 8.1: Version command
  ACTION: cd ~/dev/modastack-dogfood && modastack --version
  EXPECT: Output contains a version string (not "0.0.0-dev" if properly installed)

TEST 8.2: Doctor command
  ACTION: cd ~/dev/modastack-dogfood && modastack doctor 2>&1
  EXPECT: Output contains checkmarks or "passed"

TEST 8.3: Init command (non-interactive)
  ACTION: cd /tmp/dogfood-init-test && mkdir -p /tmp/dogfood-init-test && \
    modastack init --non-interactive 2>&1
  EXPECT: Command runs without error
  CLEANUP: rm -rf /tmp/dogfood-init-test
```

### Section 9: Monitor System

```
TEST 9.1: Monitor list
  ACTION: cd ~/dev/modastack-dogfood && modastack monitor list 2>&1
  EXPECT: Command exits successfully

TEST 9.2: Parse interval
  ACTION: python3 -c "
    import sys; sys.path.insert(0, '$HOME/dev/modastack')
    from modastack.monitors.schema import parse_interval
    print(parse_interval('5m'), parse_interval('1h'), parse_interval('30s'))
  "
  EXPECT: Output is "300 3600 30"

TEST 9.3: Invalid interval raises error
  ACTION: python3 -c "
    import sys; sys.path.insert(0, '$HOME/dev/modastack')
    from modastack.monitors.schema import parse_interval
    try:
      parse_interval('bad')
      print('NO_ERROR')
    except ValueError:
      print('RAISED')
  "
  EXPECT: Output is "RAISED"
```

### Section 10: Event Pipeline (event server → manager)

```
TEST 10.1: Start event server and manager
  ACTION:
    cd ~/dev/modastack-dogfood
    modastack event-server start 2>/dev/null
    modastack start
  WAIT: 10 seconds
  EXPECT: Both running

TEST 10.2: Webhook event reaches manager
  ACTION: Send a signed GitHub issue event via local event server webhook.
    The webhook MUST include a valid x-hub-signature-256 header computed
    with the secret from ~/.modastack/config.yaml (webhooks.secret).
    Compute HMAC-SHA256 of the raw JSON body with that secret.
    Wait 15 seconds, then check modastack log manager for evidence
    the manager received and processed it.
    Example signing (shell):
      SECRET=$(python3 -c "import yaml; print(yaml.safe_load(open('$HOME/.modastack/config.yaml')).get('webhooks',{}).get('secret',''))")
      PAYLOAD='{"action":"opened",...}'
      SIG=$(echo -n "$PAYLOAD" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')
      curl -H "x-hub-signature-256: sha256=$SIG" ...
  EXPECT: delivered_to >= 1, manager log shows the event was received

TEST 10.3: Clean up
  ACTION: cd ~/dev/modastack-dogfood && modastack stop && modastack event-server stop
  EXPECT: Both stopped
```

## Phase 4: Results and Coverage Gaps

After all tests complete:

1. **Print summary table**: Test name, status (PASS/FAIL), details for failures.

2. **For each FAIL** — red-green cycle:
   a. Diagnose the root cause. Determine if it's a test issue or a real bug.
   b. If it's a real bug, **write a test first** in `~/dev/modastack/tests/`
      that reproduces the exact failure. Run the test and **confirm it fails**
      (red). If it doesn't fail, the test isn't capturing the bug — fix the
      test until it does.
   c. **Then fix the bug** in the modastack source code.
   d. Run the test again and **confirm it passes** (green).
   e. Re-run the failing dogfood integration test to confirm it also passes now.
   f. Commit the test AND the fix together so they're linked.

3. **Print coverage gap report**: List any areas of modastack that
   have no test coverage based on the feature scan.

4. **Commit all new tests and fixes** to the modastack repo.

## Notes

- Use `source ~/dev/modastack/.venv/bin/activate` before running tests
  to ensure the dev install is available.
- All tests run from `~/dev/modastack-dogfood` unless otherwise noted.
- Tests that require a running manager should wait adequate time (5-10s)
  for initialization.
- WebSocket tests should use the `websocket-client` Python library.
- Save deployment_id and api_key from TEST 3.4 as shell variables for
  use in subsequent event server tests.
- The event server runs on port 8080 by default.
- The dogfood dashboard runs on port 8097 (configured in local.yaml).
- **Webhook signatures**: When `~/.modastack/config.yaml` has a
  `webhooks.secret` configured, the event server enforces GitHub webhook
  signature verification (`x-hub-signature-256`). Tests 3.6-3.9 work
  without signatures only when the event server is started fresh without
  a secret. For Section 10 (pipeline tests), the manager auto-starts the
  event server with the configured secret, so all webhook payloads MUST
  be HMAC-SHA256 signed. Read the secret from config and compute the
  signature before sending.
