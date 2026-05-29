# Integration Testing Guide

## Prerequisites

- modastack installed: `pip install -e .`
- tmux installed: `tmux -V`
- claude CLI authenticated (Max subscription)
- ngrok installed (for webhook tests): `ngrok http 8080`
- Slack app configured with Socket Mode
- Linear/GitHub webhooks pointed at ngrok URL

## Start the system

```bash
# 1. Start ngrok tunnel
ngrok http 8080

# 2. Get the tunnel URL
NGROK_URL=$(curl -s http://localhost:4040/api/tunnels | python3 -c "import sys,json; print(json.load(sys.stdin)['tunnels'][0]['public_url'])")
echo $NGROK_URL

# 3. Start modastack
modastack start --webhooks
```

Verify everything is up:

```bash
tmux ls                              # should show moda-manager
curl -s http://localhost:8080/health # should return {"status": "ok"}
modastack status                     # should show "No active engineers"
```

## Test each event channel

### 1. Webhook server health

```bash
curl -s http://localhost:8080/health
# Expected: {"status": "ok"}

curl -s $NGROK_URL/health
# Expected: {"status": "ok"} (proves tunnel works)
```

### 2. GitHub webhook — ping

```bash
curl -s -X POST $NGROK_URL/webhooks/github \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: ping" \
  -d '{"zen": "test ping"}'
# Expected: HTTP 200
```

### 3. GitHub webhook — PR opened

```bash
curl -s -X POST $NGROK_URL/webhooks/github \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: pull_request" \
  -d '{
    "action": "opened",
    "pull_request": {
      "number": 999,
      "title": "[TEST] integration test PR",
      "head": {"ref": "agent/test-1"},
      "state": "open",
      "merged": false,
      "html_url": "https://github.com/test/test/pull/999",
      "user": {"login": "testuser"}
    },
    "repository": {"full_name": "test/test"}
  }'
# Expected: HTTP 200, event in bus as github.pr.opened
```

### 4. GitHub webhook — PR review

```bash
curl -s -X POST $NGROK_URL/webhooks/github \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: pull_request_review" \
  -d '{
    "review": {
      "state": "changes_requested",
      "body": "Please fix the typo on line 42",
      "user": {"login": "reviewer"}
    },
    "pull_request": {
      "number": 999,
      "title": "[TEST] integration test PR"
    },
    "repository": {"full_name": "test/test"}
  }'
# Expected: HTTP 200, event in bus as github.pr.review
```

### 5. GitHub webhook — issue comment

```bash
curl -s -X POST $NGROK_URL/webhooks/github \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: issue_comment" \
  -d '{
    "comment": {
      "body": "Looks good, but can you add a test?",
      "user": {"login": "reviewer"}
    },
    "issue": {"number": 999},
    "repository": {"full_name": "test/test"}
  }'
# Expected: HTTP 200, event in bus as github.comment
```

### 6. Linear webhook — issue created

```bash
curl -s -X POST $NGROK_URL/webhooks/linear \
  -H "Content-Type: application/json" \
  -d '{
    "action": "create",
    "type": "Issue",
    "data": {
      "identifier": "TEST-1",
      "id": "fake-uuid-123",
      "title": "Test issue from integration test",
      "state": {"name": "Todo"},
      "labels": [{"name": "agent"}]
    }
  }'
# Expected: HTTP 200, event in bus as linear.issue.create
```

### 7. Linear webhook — comment added

```bash
curl -s -X POST $NGROK_URL/webhooks/linear \
  -H "Content-Type: application/json" \
  -d '{
    "action": "create",
    "type": "Comment",
    "data": {
      "body": "Approved, ship it!",
      "user": {"name": "Zach"},
      "issue": {"identifier": "TEST-1", "id": "fake-uuid-123"}
    }
  }'
# Expected: HTTP 200, event in bus as linear.comment
```

### 8. Slack Socket Mode — DM

Send a DM to Modabot in Slack. Expected:
- Event appears in bus within seconds as `slack.dm`
- "Thinking..." indicator appears in Slack
- Manager responds with a real reply

### 9. Worker poller

Create a real ticket to trigger an engineer spawn, then verify:

```bash
modastack status   # should show the active engineer
modastack events   # should show worker.waiting_input or worker.working events
```

## Verify events landed in the bus

After running the tests above:

```bash
# Show recent events
modastack events --tail 10

# Or read the raw event log
tail -10 ~/.modastack/manager/events.jsonl | python3 -c "
import sys, json
for line in sys.stdin:
    e = json.loads(line)
    detail = e['data'].get('title', '') or e['data'].get('text', '') or e['data'].get('body', '')
    print(f'{e[\"timestamp\"]} {e[\"source\"]:8s} {e[\"type\"]:30s} {detail[:60]}')
"
```

Expected event types per channel:

| Source | Event types |
|--------|------------|
| github | `github.pr.opened`, `github.pr.closed`, `github.pr.merged`, `github.pr.review`, `github.comment` |
| linear | `linear.issue.create`, `linear.issue.update`, `linear.comment` |
| slack | `slack.dm`, `slack.mention`, `slack.thread_reply` |
| worker | `worker.waiting_input`, `worker.working`, `worker.asking_question`, `worker.exited` |

## Verify manager is processing

```bash
# Check the manager session is alive
tmux ls | grep moda-manager

# Watch what the manager is doing
tmux attach -t moda-manager
# Ctrl-B D to detach

# Check recent decisions
modastack decisions

# Check the decision log
tail -3 ~/.modastack/manager/decisions.jsonl | python3 -c "
import sys, json
for line in sys.stdin:
    e = json.loads(line)
    actions = [a.get('type', '?') for a in e.get('actions', [])]
    print(f'{e[\"timestamp\"]} — {actions}')
"
```

## Full end-to-end test

The best integration test is a real task:

1. Create a Linear ticket with the `agent` label in Todo
2. Watch the manager pick it up (Slack notification)
3. Watch the engineer work (`tmux attach -t moda-<issue>`)
4. Review the PR when it's ready
5. Merge → verify the manager moves the ticket to Done

## Cleanup

```bash
# Kill simulated webhook events don't affect real state,
# but if you created real Linear tickets:
modastack status                    # check for orphaned sessions
tmux kill-session -t moda-<issue>   # kill specific engineer
tmux kill-server                    # nuclear option

# Reset state
rm -f ~/.modastack/state.json
rm -f ~/.modastack/manager/session_id
```

## Troubleshooting

**No events in bus after webhook POST:**
- Check ngrok is running: `curl -s http://localhost:4040/api/tunnels`
- Check webhook server is up: `curl -s http://localhost:8080/health`
- Check ngrok logs for errors: ngrok dashboard at http://localhost:4040

**Slack DM not received:**
- Verify Socket Mode is connected: check logs for "Slack Socket Mode connected"
- Verify bot events are subscribed: api.slack.com → Event Subscriptions
- Check if old watcher process is stealing the connection: `pgrep -f watcher`

**Manager not responding:**
- Check session is alive: `tmux ls | grep moda-manager`
- Check it's not stuck: `tmux attach -t moda-manager`
- Check for errors: `tail -20 ~/.modastack/manager.log`

**GitHub webhooks not arriving:**
- Verify webhook is registered: `gh api repos/OWNER/REPO/hooks --jq '.[].config.url'`
- Check ngrok URL matches: the URL changes when ngrok restarts
- Check delivery status: repo Settings → Webhooks → Recent Deliveries
